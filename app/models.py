"""Domain types shared by the ingest pipeline, the rule engine and the API.

Two families live here:

* **Events** - what the contact center platform emits. Parsing is deliberately
  lenient: a feed we do not control will contain nulls, unknown states and
  fields that go missing. A bad event must never take down the pipeline, so
  parsing returns a rejection instead of raising.
* **Rules** - what a user configures. A rule is a *typed* condition
  (metric / operator / threshold) rather than free-form expression, plus the
  noise controls and the audience. See `docs/DESIGN.md` for why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .clock import parse_ts

# --------------------------------------------------------------------------
# Subjects
# --------------------------------------------------------------------------


class SubjectType(str, Enum):
    QUEUE = "queue"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class Subject:
    """The entity a piece of state, a rule or a notification is about.

    (org_id, subject_type, subject_id) is also the partition key for the whole
    pipeline: every event about a subject is handled by the same worker, so
    rule state can be kept local. See docs/DESIGN.md "Scaling".
    """

    type: SubjectType
    id: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.type.value}:{self.id}"


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

KNOWN_EVENT_TYPES = ("queue_snapshot", "agent_state_change", "adherence_check")


class _Event(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str
    ts: datetime
    type: str

    @field_validator("ts", mode="before")
    @classmethod
    def _parse(cls, v: Any) -> Any:
        return parse_ts(v) if isinstance(v, str) else v


class QueueSnapshot(_Event):
    type: Literal["queue_snapshot"]
    queue_id: str
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


class AgentStateChange(_Event):
    type: Literal["agent_state_change"]
    agent_id: str
    queue_ids: list[str] | None = None
    previous_state: str | None = None
    previous_state_duration_sec: int | None = None
    new_state: str

    @property
    def subject(self) -> Subject:
        return Subject(SubjectType.AGENT, self.agent_id)


class AdherenceCheck(_Event):
    type: Literal["adherence_check"]
    agent_id: str
    queue_ids: list[str] | None = None
    scheduled_state: str | None = None
    actual_state: str | None = None
    in_violation: bool = False
    violation_started_at: datetime | None = None

    @field_validator("violation_started_at", mode="before")
    @classmethod
    def _parse_started(cls, v: Any) -> Any:
        return parse_ts(v) if isinstance(v, str) else v

    @property
    def subject(self) -> Subject:
        return Subject(SubjectType.AGENT, self.agent_id)


Event = QueueSnapshot | AgentStateChange | AdherenceCheck

_EVENT_CLASSES: dict[str, type[_Event]] = {
    "queue_snapshot": QueueSnapshot,
    "agent_state_change": AgentStateChange,
    "adherence_check": AdherenceCheck,
}


@dataclass(slots=True)
class ParsedEvent:
    """Either a valid event or the reason we could not use it."""

    raw: dict[str, Any]
    event: Event | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.event is not None


def parse_event(raw: dict[str, Any]) -> ParsedEvent:
    """Parse one event from the feed. Never raises."""
    if not isinstance(raw, dict):
        return ParsedEvent(raw={"raw": str(raw)}, error="event is not an object")
    etype = raw.get("type")
    if etype not in _EVENT_CLASSES:
        return ParsedEvent(raw=raw, error=f"unknown event type {etype!r}")
    try:
        return ParsedEvent(raw=raw, event=_EVENT_CLASSES[etype](**raw))  # type: ignore[arg-type]
    except Exception as exc:  # pydantic ValidationError and friends
        return ParsedEvent(raw=raw, error=f"invalid {etype}: {_first_error(exc)}")


def _first_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            loc = ".".join(str(p) for p in first.get("loc", ()))
            return f"{loc}: {first.get('msg')}"
        except Exception:  # pragma: no cover - defensive
            pass
    return str(exc)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


class Operator(str, Enum):
    GTE = ">="
    GT = ">"
    LTE = "<="
    LT = "<"
    EQ = "=="

    def compare(self, value: float, threshold: float) -> bool:
        if self is Operator.GTE:
            return value >= threshold
        if self is Operator.GT:
            return value > threshold
        if self is Operator.LTE:
            return value <= threshold
        if self is Operator.LT:
            return value < threshold
        return value == threshold


class ScopeMode(str, Enum):
    ALL = "all"          # every subject of this type
    IDS = "ids"          # these queue ids / agent ids
    QUEUES = "queues"    # agents serving any of these queues (agent rules only)


@dataclass(frozen=True, slots=True)
class Scope:
    mode: ScopeMode = ScopeMode.ALL
    ids: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps({"mode": self.mode.value, "ids": list(self.ids)})

    @staticmethod
    def from_json(text: str) -> "Scope":
        data = json.loads(text)
        return Scope(ScopeMode(data.get("mode", "all")), tuple(data.get("ids") or ()))


class AudienceType(str, Enum):
    USER = "user"                    # a named user
    ROLE = "role"                    # everyone with a role
    SUBJECT_AGENT = "subject_agent"  # the agent the notification is about
    QUEUE_LEADS = "queue_leads"      # leads who own the affected queue(s)


@dataclass(frozen=True, slots=True)
class AudienceTarget:
    type: AudienceType
    value: str | None = None  # user id for USER, role name for ROLE

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type.value}
        if self.value is not None:
            d["value"] = self.value
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AudienceTarget":
        return AudienceTarget(AudienceType(d["type"]), d.get("value"))


@dataclass(slots=True)
class Rule:
    """A user-configured notification rule.

    Condition:  metric <operator> threshold   (optionally filtered by agent state)
    Timing:     true for `for_sec`, clears after `clear_after_sec`,
                repeats every `renotify_sec` while still firing.
    """

    id: str
    org_id: str
    name: str
    subject_type: SubjectType
    metric: str
    operator: Operator
    threshold: float
    audience: list[AudienceTarget]
    severity: Severity = Severity.WARNING
    scope: Scope = field(default_factory=Scope)
    state_filter: str | None = None
    for_sec: int = 0
    clear_after_sec: int = 0
    renotify_sec: int = 0
    notify_on_resolve: bool = True
    enabled: bool = True
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def matches_queue_scope(self, queue_ids: list[str]) -> bool:
        return bool(set(self.scope.ids) & set(queue_ids))


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------


class Role(str, Enum):
    AGENT = "agent"
    TEAM_LEAD = "team_lead"
    HEAD_OF_SUPPORT = "head_of_support"


@dataclass(slots=True)
class User:
    """A notification recipient.

    `digest_min_severity` is the one piece of noise control that belongs to the
    person rather than the rule: a head of support subscribes to the same rules
    as a team lead but only wants to be interrupted when something is on fire,
    and everything quieter arrives as a periodic roll-up.
    """

    id: str
    org_id: str
    name: str
    role: Role
    agent_id: str | None = None
    queue_ids: list[str] = field(default_factory=list)
    channel: str = "slack"
    digest_min_severity: Severity = Severity.INFO
    digest_interval_sec: int = 900

    def wants_immediately(self, severity: Severity) -> bool:
        return severity.rank >= self.digest_min_severity.rank


@dataclass(slots=True)
class Notification:
    """One rendered message for one recipient."""

    id: str
    org_id: str
    dedupe_key: str
    created_at: datetime
    kind: str            # fire | reminder | resolve | digest
    severity: Severity
    recipient_id: str
    channel: str
    title: str
    body: str
    delivery: str        # immediate | digest
    status: str          # delivered | buffered | rolled_up
    rule_id: str | None = None
    rule_name: str | None = None
    incident_id: str | None = None
    subject_type: SubjectType | None = None
    subject_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    delivered_at: datetime | None = None
