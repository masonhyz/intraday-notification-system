"""The rule engine: from a stream of events to notifications.

    ingest(event) -> update subject state -> evaluate that subject's rules
    tick(now)     -> re-evaluate anything time-sensitive, flush digests

The important idea is that a rule does not fire an event; it *opens and closes
an incident*. Each (rule, subject) pair owns a small state machine:

    ok ──condition true──► pending ──held for `for_sec`──► firing
     ▲                        │                             │
     └────condition false─────┘                  condition false
                                                          │
                              ok ◄──quiet for `clear_after_sec`── clearing

Everything people hate about alerting falls out of that shape:

* a queue oscillating around its threshold produces one incident, not twelve
  (`for_sec` to fire, `clear_after_sec` to resolve);
* while an incident is open there is at most one reminder per `renotify_sec`,
  not one per 30-second snapshot;
* the recipient is told when it is over, so nobody has to poll a dashboard to
  find out whether it is still bad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from .clock import Clock, ManualClock, SystemClock
from .metrics import MetricDef, get_metric
from .models import (
    Event,
    ParsedEvent,
    Rule,
    ScopeMode,
    Subject,
    SubjectType,
    parse_event,
)
from .notify import FIRE, REMINDER, RESOLVE, Channel, Emission, Notifier, render, subject_label
from .routing import resolve_audience
from .rules import describe
from .state import AgentState, EntityState, apply_event
from .store import RuleSubjectState, Store, new_id

OK, PENDING, FIRING, CLEARING = "ok", "pending", "firing", "clearing"

#: How long a subject can go unheard-from before we stop drawing conclusions
#: about it. Alerting off a frozen picture is worse than not alerting.
#:
#: The two horizons differ because the two feeds mean different things. A queue
#: snapshot is a *sampled measurement* arriving every ~30s: once it stops, the
#: numbers are simply unknown. An agent's state is an *edge-triggered fact* -
#: they are on that call until something says otherwise - so it stays usable
#: far longer, which is what lets a long-call alert fire mid-call.
DEFAULT_STALENESS_SEC: Mapping[SubjectType, int] = {
    SubjectType.QUEUE: 15 * 60,
    SubjectType.AGENT: 2 * 60 * 60,
}


@dataclass(slots=True)
class IngestResult:
    accepted: bool
    status: str                       # applied | stale | rejected | duplicate
    note: str | None = None
    subject: Subject | None = None
    notifications: list[Any] = field(default_factory=list)


class Engine:
    """Owns ingest, evaluation and dispatch for one org."""

    def __init__(
        self,
        store: Store,
        *,
        clock: Clock | None = None,
        channels: Iterable[Channel] = (),
        notifier: Notifier | None = None,
        staleness_sec: Mapping[SubjectType, int] = DEFAULT_STALENESS_SEC,
    ) -> None:
        self.store = store
        self.clock = clock or SystemClock()
        self.notifier = notifier or Notifier(store, channels)
        #: Pass `{}` to evaluate regardless of how old the state is.
        self.staleness_sec = dict(staleness_sec)

    # -- ingest ------------------------------------------------------------

    def ingest(self, raw: dict[str, Any]) -> IngestResult:
        """Handle one event end to end. Safe to call with anything."""
        parsed: ParsedEvent = parse_event(raw)
        received_at = self._advance_to(parsed.event.ts if parsed.ok else None)

        if not parsed.ok:
            self.store.events.record(
                event_id=str(raw.get("event_id") or new_id("evt")),
                ts=None,
                received_at=received_at,
                type_=str(raw.get("type") or "unknown"),
                subject=None,
                status="rejected",
                note=parsed.error,
                payload=raw,
            )
            return IngestResult(accepted=False, status="rejected", note=parsed.error)

        event: Event = parsed.event
        subject = event.subject
        current = self.store.state.get(subject)
        result = apply_event(current, event, self.store.org_id)

        recorded = self.store.events.record(
            event_id=event.event_id,
            ts=event.ts,
            received_at=received_at,
            type_=event.type,
            subject=subject,
            status="applied" if result.applied else "stale",
            note=result.note,
            payload=raw,
        )
        if not recorded:
            # Same event_id seen before. At-least-once producers redeliver; this
            # must not re-apply state or re-fire rules.
            return IngestResult(
                accepted=False, status="duplicate", subject=subject,
                note="event_id already ingested",
            )
        if not result.applied:
            return IngestResult(accepted=True, status="stale", subject=subject, note=result.note)

        self.store.state.save(result.state)
        notifications = self._evaluate_subject(subject, result.state, received_at)
        return IngestResult(
            accepted=True, status="applied", subject=subject,
            note=result.note, notifications=notifications,
        )

    def ingest_many(self, raws: Iterable[dict[str, Any]]) -> list[IngestResult]:
        return [self.ingest(raw) for raw in raws]

    # -- time --------------------------------------------------------------

    def tick(self, now: datetime | None = None) -> list[Any]:
        """Re-evaluate what the passage of time alone can change.

        Two things need this: metrics that grow with the clock (an agent is
        still on the same call), and deadlines on incidents already in flight
        (`for_sec` elapsed, `clear_after_sec` elapsed, a reminder due).
        """
        now = self._advance_to(now)
        notifications: list[Any] = []

        for rule in self.store.rules.list(enabled_only=True):
            metric = get_metric(rule.metric)
            if metric.time_varying:
                for state in self.store.state.all_of(rule.subject_type):
                    if self._rule_covers(rule, state):
                        notifications += self._evaluate(rule, state, now)

        # Rules whose metric only moves on new events can still have a pending
        # or clearing deadline come due.
        for rule_id, subject_id in self.store.engine_state.active_pairs():
            rule = self.store.rules.get(rule_id)
            if rule is None or not rule.enabled or get_metric(rule.metric).time_varying:
                continue
            state = self.store.state.get(Subject(rule.subject_type, subject_id))
            if state is not None:
                notifications += self._evaluate(rule, state, now)

        notifications += self.notifier.flush_digests(now)
        return notifications

    def _advance_to(self, ts: datetime | None) -> datetime:
        """Move a replay clock forward to the event, never backwards.

        Late events therefore evaluate at the current watermark rather than
        resurrecting a moment that has already passed.
        """
        if ts is not None and isinstance(self.clock, ManualClock):
            self.clock.set(ts)
        return self.clock.now()

    # -- evaluation --------------------------------------------------------

    def _evaluate_subject(
        self, subject: Subject, state: EntityState, now: datetime
    ) -> list[Any]:
        """Evaluate only the rules that can possibly care about this subject.

        This is the hot path: cost is proportional to the rules watching one
        queue or one agent, not to the size of the rule book.
        """
        notifications: list[Any] = []
        for rule in self.store.rules.list(subject_type=subject.type, enabled_only=True):
            if self._rule_covers(rule, state):
                notifications += self._evaluate(rule, state, now)
        return notifications

    @staticmethod
    def _rule_covers(rule: Rule, state: EntityState) -> bool:
        subject_id = state.subject.id
        if rule.scope.mode is ScopeMode.ALL:
            return True
        if rule.scope.mode is ScopeMode.IDS:
            return subject_id in rule.scope.ids
        if rule.scope.mode is ScopeMode.QUEUES and isinstance(state, AgentState):
            return bool(set(rule.scope.ids) & set(state.queue_ids))
        return False

    def _is_stale(self, state: EntityState, now: datetime) -> bool:
        horizon = self.staleness_sec.get(state.subject.type)
        if horizon is None:
            return False
        return (now - state.updated_at).total_seconds() > horizon

    def _evaluate(self, rule: Rule, state: EntityState, now: datetime) -> list[Any]:
        if self._is_stale(state, now):
            # Freeze: do not fire, and do not resolve either. Silence is not
            # recovery, so an incident that is already open stays open until we
            # hear something that actually clears it.
            return []

        metric = get_metric(rule.metric)
        value = self._metric_value(rule, metric, state, now)
        condition = value is not None and rule.operator.compare(value, rule.threshold)

        subject = state.subject
        rss = self.store.engine_state.get(rule.id, subject.id) or RuleSubjectState(
            rule.id, self.store.org_id, subject.id
        )
        emissions = self._transition(rule, rss, condition, value, now)
        rss.last_value = value
        rss.last_eval_at = now
        self.store.engine_state.save(rss)

        out: list[Any] = []
        for emission in emissions:
            out += self._deliver(emission, metric, state)
        return out

    @staticmethod
    def _metric_value(
        rule: Rule, metric: MetricDef, state: EntityState, now: datetime
    ) -> float | None:
        if rule.state_filter and metric.supports_state_filter:
            # "on a call for 45 minutes" must not keep counting once the agent
            # hangs up, even though the underlying timer is the same one.
            if not isinstance(state, AgentState) or state.state != rule.state_filter:
                return None
        return metric.compute(state, now)

    def _transition(
        self,
        rule: Rule,
        rss: RuleSubjectState,
        condition: bool,
        value: float | None,
        now: datetime,
    ) -> list[Emission]:
        """Advance the (rule, subject) state machine. Pure apart from `rss`."""
        emissions: list[Emission] = []

        def fire(kind: str) -> None:
            emissions.append(
                Emission(
                    kind=kind,
                    rule=rule,
                    subject=Subject(rule.subject_type, rss.subject_id),
                    incident_id=rss.incident_id or "",
                    at=now,
                    value=value,
                    condition_since=rss.condition_since,
                    opened_at=rss.opened_at,
                    seq=rss.notify_seq,
                )
            )
            rss.last_notified_at = now
            rss.notify_seq += 1

        def open_incident() -> None:
            rss.status = FIRING
            rss.incident_id = new_id("inc")
            rss.opened_at = now
            rss.notify_seq = 0
            rss.clear_since = None
            fire(FIRE)

        if rss.status == OK:
            if condition:
                rss.condition_since = now
                if rule.for_sec <= 0:
                    open_incident()
                else:
                    rss.status = PENDING

        elif rss.status == PENDING:
            if not condition:
                rss.status = OK
                rss.condition_since = None
            elif (now - rss.condition_since).total_seconds() >= rule.for_sec:
                open_incident()

        elif rss.status == FIRING:
            if condition:
                if rule.renotify_sec and rss.last_notified_at is not None:
                    if (now - rss.last_notified_at).total_seconds() >= rule.renotify_sec:
                        fire(REMINDER)
            elif rule.clear_after_sec > 0:
                rss.status = CLEARING
                rss.clear_since = now
            else:
                self._close(rss, rule, fire)

        elif rss.status == CLEARING:
            if condition:
                # Flapped back before the all-clear: same incident, no new ping.
                rss.status = FIRING
                rss.clear_since = None
            elif (now - rss.clear_since).total_seconds() >= rule.clear_after_sec:
                self._close(rss, rule, fire)

        return emissions

    @staticmethod
    def _close(rss: RuleSubjectState, rule: Rule, fire) -> None:
        if rule.notify_on_resolve and rss.incident_id:
            fire(RESOLVE)
        rss.status = OK
        rss.condition_since = None
        rss.clear_since = None
        rss.incident_id = None
        rss.opened_at = None
        rss.notify_seq = 0

    # -- dispatch ----------------------------------------------------------

    def _deliver(self, emission: Emission, metric: MetricDef, state: EntityState) -> list[Any]:
        rule = emission.rule
        recipients = resolve_audience(rule, emission.subject, state, self.store.users)
        if not recipients:
            return []

        names = {u.id: u.name for u in self.store.users.list()}
        sentence = describe(rule, names)
        agent_user = (
            self.store.users.by_agent_id(emission.subject.id)
            if emission.subject.type is SubjectType.AGENT
            else None
        )
        label = subject_label(emission.subject, state, agent_user)
        title, body, context = render(emission, metric, state, label, sentence)

        sent = []
        for recipient in recipients:
            notification = self.notifier.dispatch(
                recipient=recipient,
                emission=emission,
                title=title,
                body=body,
                context=context,
                severity=rule.severity,
                kind=emission.kind,
                at=emission.at,
                dedupe_key=f"{emission.dedupe_key}:{recipient.id}",
                subject=emission.subject,
                rule=rule,
            )
            if notification is not None:
                sent.append(notification)
        return sent
