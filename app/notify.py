"""Turning a fired rule into a message, and getting that message to a person.

Three separable jobs, kept separate:

1. **Render** - facts first. Every notification answers "what is wrong, how bad,
   and why am I being told?" in its first two lines, because the reader is
   looking at it on a phone in the middle of a shift.
2. **Route to a channel** - real Slack/email/push are out of scope, so channels
   are stubs behind one interface. Swapping in a real transport is one class.
3. **Hold back the noise** - a recipient whose `digest_min_severity` is above a
   notification's severity gets it rolled into a periodic summary instead of an
   interruption. This is the piece that makes the head-of-support experience
   work without a separate rule set.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Protocol

from .clock import iso
from .metrics import MetricDef, format_duration, format_value
from .models import Notification, Rule, Severity, Subject, SubjectType, User
from .rules import describe_threshold_phrase
from .state import AgentState, EntityState, QueueState
from .store import Store, new_id

FIRE = "fire"
REMINDER = "reminder"
RESOLVE = "resolve"
DIGEST = "digest"


@dataclass(slots=True)
class Emission:
    """The engine's decision that something should be said, before we say it."""

    kind: str                    # fire | reminder | resolve
    rule: Rule
    subject: Subject
    incident_id: str
    at: datetime                 # event time this was decided at
    value: float | None          # metric value now
    condition_since: datetime | None
    opened_at: datetime | None
    seq: int = 0                 # 0 for the first notification of an incident

    @property
    def dedupe_key(self) -> str:
        """Stable across re-evaluation: replaying the same events twice cannot
        produce a second copy of the same notification."""
        return f"{self.incident_id}:{self.kind}:{self.seq}"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_KIND_PREFIX = {FIRE: "", REMINDER: "Still firing: ", RESOLVE: "Recovered: "}


def subject_label(subject: Subject, state: EntityState | None, user: User | None) -> str:
    if subject.type is SubjectType.AGENT and user is not None:
        return f"{subject.id} ({user.name})"
    return subject.id


def render(
    emission: Emission,
    metric: MetricDef,
    state: EntityState | None,
    label: str,
    rule_sentence: str,
) -> tuple[str, str, dict]:
    """Return (title, body, context) for one emission."""
    rule = emission.rule
    value_text = format_value(emission.value, metric.unit)
    title = f"{_KIND_PREFIX[emission.kind]}{rule.name} — {label}"

    # "time in current state" is the metric; "time on call" is what the rule
    # actually watches. Say the second one.
    measure = metric.label.lower()
    if rule.state_filter and metric.supports_state_filter:
        measure = f"time {rule.state_filter.replace('_', ' ')}"

    held = _held_for(emission)
    if emission.kind == RESOLVE:
        # The metric often stops applying entirely on recovery (an agent who is
        # back in adherence has no violation to measure), so only quote a value
        # when there is one.
        lead = (
            f"{label} is back within range ({measure} now {value_text})."
            if emission.value is not None
            else f"{label} is back within range."
        )
        if held:
            lead += f" The alert was open for {held}."
    else:
        lead = (
            f"{label}: {measure} is {value_text}, "
            f"threshold is {describe_threshold_phrase(rule)}."
        )
        if held:
            lead += f" Has been true for {held}."

    body_lines = [lead]
    detail = _context_line(state)
    if detail:
        body_lines.append(detail)
    body_lines.append(f"Why: {rule_sentence}")

    context = {
        "metric": metric.key,
        "value": emission.value,
        "unit": metric.unit,
        "threshold": rule.threshold,
        "operator": rule.operator.value,
        "condition_since": iso(emission.condition_since),
        "opened_at": iso(emission.opened_at),
        "state": _state_context(state),
    }
    return title, "\n".join(body_lines), context


def _held_for(emission: Emission) -> str | None:
    start = emission.opened_at if emission.kind == RESOLVE else emission.condition_since
    if start is None:
        return None
    seconds = (emission.at - start).total_seconds()
    return format_duration(seconds) if seconds >= 30 else None


def _context_line(state: EntityState | None) -> str | None:
    """A single line of the surrounding situation, so the reader can act
    without opening another tab."""
    if isinstance(state, QueueState):
        bits = []
        if state.tickets_waiting is not None:
            bits.append(f"{state.tickets_waiting} waiting")
        if state.longest_wait_sec is not None:
            bits.append(f"longest {format_duration(state.longest_wait_sec)}")
        if state.sla_target_sec is not None:
            bits.append(f"SLA {format_duration(state.sla_target_sec)}")
        if state.agents_available is not None:
            bits.append(f"{state.agents_available} available")
        if state.agents_on_call is not None:
            bits.append(f"{state.agents_on_call} on call")
        if state.volume_last_15m is not None and state.volume_forecast_next_15m:
            bits.append(f"volume {state.volume_last_15m} vs {state.volume_forecast_next_15m} forecast")
        return " · ".join(bits) or None
    if isinstance(state, AgentState):
        bits = []
        if state.state:
            bits.append(f"state {state.state}")
        if state.scheduled_state:
            bits.append(f"scheduled {state.scheduled_state}")
        if state.queue_ids:
            bits.append("queues " + ", ".join(state.queue_ids))
        return " · ".join(bits) or None
    return None


def _state_context(state: EntityState | None) -> dict:
    if isinstance(state, QueueState):
        return {
            "tickets_waiting": state.tickets_waiting,
            "longest_wait_sec": state.longest_wait_sec,
            "sla_target_sec": state.sla_target_sec,
            "agents_available": state.agents_available,
            "agents_on_call": state.agents_on_call,
        }
    if isinstance(state, AgentState):
        return {
            "state": state.state,
            "state_since": iso(state.state_since),
            "scheduled_state": state.scheduled_state,
            "queue_ids": state.queue_ids,
            "in_violation": state.in_violation,
        }
    return {}


# --------------------------------------------------------------------------
# Channels (stubs - see docs/DESIGN.md "Out of scope")
# --------------------------------------------------------------------------


class Channel(Protocol):
    name: str

    def deliver(self, notification: Notification) -> None: ...


class ConsoleChannel:
    """Prints notifications as they fire. The default for the replay CLI."""

    name = "console"

    def __init__(self, stream=None) -> None:
        import sys

        self.stream = stream or sys.stdout

    def deliver(self, n: Notification) -> None:
        when = iso(n.created_at)[11:19]
        tag = n.severity.value.upper()
        head = f"[{when}] {tag:<8} → {n.recipient_id:<16} via {n.channel:<7} {n.title}"
        indent = "             "
        print(head, file=self.stream)
        for line in n.body.splitlines():
            print(f"{indent}{line}", file=self.stream)
        self.stream.flush()


class FileChannel:
    """Appends one JSON object per notification - a stand-in for a real sink."""

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def deliver(self, n: Notification) -> None:
        record = {
            "at": iso(n.created_at),
            "recipient": n.recipient_id,
            "channel": n.channel,
            "severity": n.severity.value,
            "kind": n.kind,
            "rule": n.rule_name,
            "subject": f"{n.subject_type.value}:{n.subject_id}" if n.subject_type else None,
            "title": n.title,
            "body": n.body,
        }
        with self.path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")


class CollectingChannel:
    """Keeps everything in memory. Used by tests and the /notifications API."""

    name = "memory"

    def __init__(self) -> None:
        self.delivered: list[Notification] = []

    def deliver(self, n: Notification) -> None:
        self.delivered.append(n)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


class Notifier:
    """Persists notifications and hands them to the configured channels."""

    def __init__(self, store: Store, channels: Iterable[Channel] = ()) -> None:
        self.store = store
        self.channels = list(channels)

    def dispatch(
        self,
        *,
        recipient: User,
        emission: Emission | None,
        title: str,
        body: str,
        context: dict,
        severity: Severity,
        kind: str,
        at: datetime,
        dedupe_key: str,
        subject: Subject | None = None,
        rule: Rule | None = None,
    ) -> Notification | None:
        """Deliver, or hold for the digest. Returns None if already sent."""
        immediate = recipient.wants_immediately(severity)
        notification = Notification(
            id=new_id("ntf"),
            org_id=self.store.org_id,
            dedupe_key=dedupe_key,
            created_at=at,
            kind=kind,
            severity=severity,
            recipient_id=recipient.id,
            channel=recipient.channel if immediate else "digest",
            title=title,
            body=body,
            delivery="immediate" if immediate else "digest",
            status="delivered" if immediate else "buffered",
            rule_id=rule.id if rule else None,
            rule_name=rule.name if rule else None,
            incident_id=emission.incident_id if emission else None,
            subject_type=subject.type if subject else None,
            subject_id=subject.id if subject else None,
            context=context,
            delivered_at=at if immediate else None,
        )
        if not self.store.notifications.insert(notification):
            return None  # already sent; at-least-once evaluation is safe
        if immediate:
            for channel in self.channels:
                channel.deliver(notification)
        return notification

    # -- digest ------------------------------------------------------------

    def flush_digests(self, now: datetime, *, force: bool = False) -> list[Notification]:
        """Roll buffered notifications up into one summary per recipient.

        A recipient's digest clock starts with their oldest held notification,
        not on a global schedule, so the first quiet alert sets the timer and
        nothing waits longer than the configured interval.
        """
        sent: list[Notification] = []
        by_recipient: dict[str, list[Notification]] = defaultdict(list)
        for n in self.store.notifications.buffered():
            by_recipient[n.recipient_id].append(n)

        for recipient_id, held in by_recipient.items():
            user = self.store.users.get(recipient_id)
            if user is None:  # pragma: no cover - defensive
                continue
            oldest = min(n.created_at for n in held)
            due = oldest + timedelta(seconds=user.digest_interval_sec)
            if not force and now < due:
                continue

            title, body = _render_digest(held, now - oldest)
            digest = Notification(
                id=new_id("ntf"),
                org_id=self.store.org_id,
                dedupe_key=f"digest:{recipient_id}:{iso(now)}",
                created_at=now,
                kind=DIGEST,
                severity=max((n.severity for n in held), key=lambda s: s.rank),
                recipient_id=recipient_id,
                channel=user.channel,
                title=title,
                body=body,
                delivery="immediate",
                status="delivered",
                context={"rolled_up": [n.id for n in held], "count": len(held)},
                delivered_at=now,
            )
            if not self.store.notifications.insert(digest):
                continue
            self.store.notifications.mark_rolled_up([n.id for n in held], now)
            for channel in self.channels:
                channel.deliver(digest)
            sent.append(digest)
        return sent


def _render_digest(held: list[Notification], window: timedelta) -> tuple[str, str]:
    """Collapse held notifications to one line per incident.

    Something that flared and recovered inside the digest window is not
    something the reader needs to go and do - it belongs under "sorted itself
    out", not at the top of the list. Reminders for an incident collapse into
    its single line too.
    """
    incidents: dict[str, list[Notification]] = defaultdict(list)
    order: list[str] = []
    for n in held:
        key = n.incident_id or n.id
        if key not in incidents:
            order.append(key)
        incidents[key].append(n)

    still_open: list[str] = []
    settled: list[str] = []
    for key in order:
        group = incidents[key]
        first = group[0]
        resolve = next((n for n in group if n.kind == RESOLVE), None)
        headline = first.title.split(": ", 1)[-1] if first.kind != FIRE else first.title
        # Two incidents from the same rule can land in one digest, so stamp each
        # line with when it started.
        started = iso(first.created_at)[11:16]
        if resolve is not None:
            span = resolve.created_at - first.created_at
            suffix = f", lasted {format_duration(span.total_seconds())}" if span else ""
            settled.append(f"  • {started} {headline}{suffix}")
        else:
            still_open.append(f"  • {started} [{first.severity.value}] {headline}")

    title = f"Digest: {len(still_open)} open, {len(settled)} recovered"
    lines = [f"Summary of the last {format_duration(window.total_seconds())}."]
    if still_open:
        lines += ["", "Still needing attention:", *still_open]
    if settled:
        lines += ["", "Flared and recovered on their own:", *settled]
    return title, "\n".join(lines)
