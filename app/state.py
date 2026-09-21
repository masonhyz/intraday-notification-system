"""Derived entity state: the small, current picture of each queue and agent.

The raw event log is append-only and unbounded; rules are evaluated against
this compact per-subject state instead. One row per queue, one per agent,
regardless of event volume - which is what keeps evaluation cheap at millions
of events a day.

Applying an event is a pure function of (old state, event). It is deliberately
defensive about a feed we do not control:

* **Late events.** Each stream carries its own watermark, so a snapshot that
  arrives out of order cannot roll current state backwards - and a late
  adherence check cannot block a fresh state change.
* **Missing fields.** `queue_ids: null` or `[]` keeps the last known routing
  instead of orphaning the agent from its queues.
* **Inconsistent fields.** `in_violation: true` with no `violation_started_at`
  falls back to the event timestamp (a lower bound on the violation).
* **Missed transitions.** An adherence check reporting an `actual_state` we
  never saw a transition for reconciles the agent's state, treating the check
  time as a lower bound for `state_since`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import AdherenceCheck, AgentStateChange, Event, QueueSnapshot, Subject, SubjectType


@dataclass(slots=True)
class QueueState:
    org_id: str
    queue_id: str
    updated_at: datetime
    tickets_waiting: int | None = None
    longest_wait_sec: int | None = None
    sla_target_sec: int | None = None
    agents_available: int | None = None
    agents_on_call: int | None = None
    volume_last_15m: int | None = None
    volume_forecast_next_15m: int | None = None

    @property
    def subject(self) -> Subject:
        return Subject(SubjectType.QUEUE, self.queue_id)


@dataclass(slots=True)
class AgentState:
    org_id: str
    agent_id: str
    updated_at: datetime
    state: str | None = None
    state_since: datetime | None = None
    state_updated_at: datetime | None = None
    queue_ids: list[str] = field(default_factory=list)
    scheduled_state: str | None = None
    actual_state: str | None = None
    in_violation: bool = False
    violation_started_at: datetime | None = None
    adherence_updated_at: datetime | None = None

    @property
    def subject(self) -> Subject:
        return Subject(SubjectType.AGENT, self.agent_id)


EntityState = QueueState | AgentState


@dataclass(slots=True)
class ApplyResult:
    state: EntityState
    applied: bool
    note: str | None = None


def apply_event(current: EntityState | None, event: Event, org_id: str) -> ApplyResult:
    """Fold one event into the subject's state."""
    if isinstance(event, QueueSnapshot):
        return _apply_queue_snapshot(current, event, org_id)  # type: ignore[arg-type]
    if isinstance(event, AgentStateChange):
        return _apply_state_change(current, event, org_id)  # type: ignore[arg-type]
    if isinstance(event, AdherenceCheck):
        return _apply_adherence(current, event, org_id)  # type: ignore[arg-type]
    raise TypeError(f"unhandled event type {type(event).__name__}")  # pragma: no cover


def _apply_queue_snapshot(
    current: QueueState | None, event: QueueSnapshot, org_id: str
) -> ApplyResult:
    if current is None:
        current = QueueState(org_id=org_id, queue_id=event.queue_id, updated_at=event.ts)
    elif event.ts < current.updated_at:
        # A snapshot older than what we already have. Recorded, but it must not
        # roll the live picture backwards.
        return ApplyResult(current, applied=False, note="stale snapshot")

    for attr in (
        "tickets_waiting",
        "longest_wait_sec",
        "sla_target_sec",
        "agents_available",
        "agents_on_call",
        "volume_last_15m",
        "volume_forecast_next_15m",
    ):
        value = getattr(event, attr)
        # `None` means "not reported in this snapshot", not "zero": keep the
        # last known value rather than inventing one.
        if value is not None:
            setattr(current, attr, value)
    current.updated_at = event.ts
    return ApplyResult(current, applied=True)


def _apply_state_change(
    current: AgentState | None, event: AgentStateChange, org_id: str
) -> ApplyResult:
    if current is None:
        current = AgentState(org_id=org_id, agent_id=event.agent_id, updated_at=event.ts)
    elif current.state_updated_at is not None and event.ts < current.state_updated_at:
        return ApplyResult(current, applied=False, note="stale state change")

    if event.queue_ids:  # null or [] -> keep last known routing
        current.queue_ids = list(event.queue_ids)
    if current.state != event.new_state or current.state_since is None:
        current.state_since = event.ts
    current.state = event.new_state
    current.state_updated_at = event.ts
    current.updated_at = max(current.updated_at, event.ts)
    return ApplyResult(current, applied=True)


def _apply_adherence(
    current: AgentState | None, event: AdherenceCheck, org_id: str
) -> ApplyResult:
    if current is None:
        current = AgentState(org_id=org_id, agent_id=event.agent_id, updated_at=event.ts)
    elif current.adherence_updated_at is not None and event.ts < current.adherence_updated_at:
        return ApplyResult(current, applied=False, note="stale adherence check")

    if event.queue_ids:
        current.queue_ids = list(event.queue_ids)
    current.scheduled_state = event.scheduled_state
    current.actual_state = event.actual_state
    current.in_violation = bool(event.in_violation)
    if event.in_violation:
        # An open violation with no start time is still a violation. Fall back to
        # the check timestamp, which under-counts rather than over-counts.
        current.violation_started_at = event.violation_started_at or current.violation_started_at or event.ts
    else:
        current.violation_started_at = None

    note = None
    if event.actual_state and event.actual_state != current.state:
        # We never saw the transition (dropped event, or the agent moved between
        # checks). Reconcile; `state_since` becomes a lower bound.
        if current.state_updated_at is None or event.ts >= current.state_updated_at:
            note = f"reconciled state {current.state!r} -> {event.actual_state!r}"
            current.state = event.actual_state
            current.state_since = event.ts
            current.state_updated_at = event.ts

    current.adherence_updated_at = event.ts
    current.updated_at = max(current.updated_at, event.ts)
    return ApplyResult(current, applied=True, note=note)
