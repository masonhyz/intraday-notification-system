"""The metric catalog: the vocabulary a rule can be written against.

A rule is `metric <operator> threshold`, where `metric` comes from this fixed,
self-describing catalog rather than a free-form expression language. That is a
deliberate trade (see docs/DESIGN.md):

* the rule builder UI is generated from the catalog - units, help text,
  sensible defaults and validation all come for free;
* every rule is cheap and safe to evaluate (no parser, no sandbox, no
  unbounded query), which is what makes "evaluate on every event" viable;
* adding a signal is one entry here plus one row of state.

A metric returns `None` when it does not apply to the subject right now (the
queue has never reported a forecast, the agent is not in violation). `None` is
*not* zero: the condition is simply false and no incident opens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from .models import SubjectType
from .state import AgentState, EntityState, QueueState

Unit = Literal["count", "seconds", "ratio"]


@dataclass(frozen=True, slots=True)
class MetricDef:
    key: str
    subject_type: SubjectType
    label: str
    unit: Unit
    description: str
    compute: Callable[[EntityState, datetime], float | None]
    #: True when the value changes with the passage of time alone, so the rule
    #: must be re-evaluated on a tick and not only when an event arrives.
    time_varying: bool = False
    #: True when the metric can be narrowed to one agent state
    #: (e.g. "time on a *call*"), via the rule's `state_filter`.
    supports_state_filter: bool = False
    #: Sentence fragment used to describe a rule in plain English, with
    #: `{op}` ("at least") and `{value}` ("20 tickets waiting") filled in.
    phrase: str = "has {op} {value}"
    #: Variant used when the rule carries a `state_filter`; `{state}` is filled in.
    phrase_with_state: str | None = None
    #: Singular/plural noun for count metrics, so the copy reads
    #: "1 agent available" rather than "1 agents available".
    count_noun: tuple[str, str] | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "subject_type": self.subject_type.value,
            "label": self.label,
            "unit": self.unit,
            "description": self.description,
            "time_varying": self.time_varying,
            "supports_state_filter": self.supports_state_filter,
        }


def _elapsed(since: datetime | None, now: datetime) -> float | None:
    if since is None:
        return None
    return max(0.0, (now - since).total_seconds())


# --- queue metrics --------------------------------------------------------


def _tickets_waiting(s: QueueState, now: datetime) -> float | None:
    return None if s.tickets_waiting is None else float(s.tickets_waiting)


def _longest_wait(s: QueueState, now: datetime) -> float | None:
    return None if s.longest_wait_sec is None else float(s.longest_wait_sec)


def _sla_ratio(s: QueueState, now: datetime) -> float | None:
    """Oldest ticket's wait as a fraction of the queue's SLA target.

    One metric covers the whole SLA story: 0.8 is "at risk", >= 1.0 is
    "breached". Expressing it as a ratio means a single rule works across
    queues whose targets differ by an order of magnitude (vip 60s, tier_2 300s).
    """
    if s.longest_wait_sec is None or not s.sla_target_sec:
        return None
    return s.longest_wait_sec / s.sla_target_sec


def _agents_available(s: QueueState, now: datetime) -> float | None:
    return None if s.agents_available is None else float(s.agents_available)


def _volume_vs_forecast(s: QueueState, now: datetime) -> float | None:
    """Observed volume against the forecast, as a ratio (1.4 == 40% over).

    Simplification: we compare the last 15 minutes of volume against the
    forecast attached to the same snapshot, which is the forecast for the *next*
    15 minutes. The strictly correct comparison is against the forecast made
    15 minutes ago; doing that needs a short per-queue history, which is a
    natural extension of this state row but not worth it for a first cut.
    """
    if not s.volume_forecast_next_15m or s.volume_last_15m is None:
        return None
    return s.volume_last_15m / s.volume_forecast_next_15m


# --- agent metrics --------------------------------------------------------


def _time_in_state(s: AgentState, now: datetime) -> float | None:
    return _elapsed(s.state_since, now)


def _adherence_violation_sec(s: AgentState, now: datetime) -> float | None:
    if not s.in_violation:
        return None
    return _elapsed(s.violation_started_at, now)


CATALOG: dict[str, MetricDef] = {
    m.key: m
    for m in (
        MetricDef(
            "tickets_waiting",
            SubjectType.QUEUE,
            "Tickets waiting",
            "count",
            "How many tickets are currently unanswered in the queue.",
            _tickets_waiting,
            count_noun=("ticket waiting", "tickets waiting"),
        ),
        MetricDef(
            "longest_wait_sec",
            SubjectType.QUEUE,
            "Longest wait",
            "seconds",
            "How long the oldest unanswered ticket has been waiting.",
            _longest_wait,
            phrase="has a ticket that has been waiting {op} {value}",
        ),
        MetricDef(
            "sla_ratio",
            SubjectType.QUEUE,
            "SLA consumed",
            "ratio",
            "Longest wait as a fraction of the queue's SLA target. 0.8 = at risk, 1.0 = breached.",
            _sla_ratio,
            phrase="has burned {op} {value} of its SLA target",
        ),
        MetricDef(
            "agents_available",
            SubjectType.QUEUE,
            "Agents available",
            "count",
            "Agents on this queue who are free to take work right now.",
            _agents_available,
            count_noun=("agent available", "agents available"),
        ),
        MetricDef(
            "volume_vs_forecast",
            SubjectType.QUEUE,
            "Volume vs forecast",
            "ratio",
            "Recent volume divided by the forecast. 1.5 = running 50% hotter than expected.",
            _volume_vs_forecast,
            phrase="is taking {op} {value} of its forecast volume",
        ),
        MetricDef(
            "time_in_state_sec",
            SubjectType.AGENT,
            "Time in current state",
            "seconds",
            "How long the agent has been in one state - counted live, so it fires "
            "during a long call rather than after it ends.",
            _time_in_state,
            time_varying=True,
            supports_state_filter=True,
            phrase="has been in the same state for {op} {value}",
            phrase_with_state="has been {state} for {op} {value}",
        ),
        MetricDef(
            "adherence_violation_sec",
            SubjectType.AGENT,
            "Time out of adherence",
            "seconds",
            "How long the agent has been doing something other than what they are scheduled for.",
            _adherence_violation_sec,
            time_varying=True,
            phrase="has been out of adherence for {op} {value}",
        ),
    )
}


def get_metric(key: str) -> MetricDef:
    try:
        return CATALOG[key]
    except KeyError:
        raise KeyError(f"unknown metric {key!r}; known: {sorted(CATALOG)}") from None


def metrics_for(subject_type: SubjectType) -> list[MetricDef]:
    return [m for m in CATALOG.values() if m.subject_type is subject_type]


def format_value(value: float | None, unit: Unit) -> str:
    """Human formatting used in notification copy and the UI."""
    if value is None:
        return "n/a"
    if unit == "seconds":
        return format_duration(value)
    if unit == "ratio":
        return f"{value * 100:.0f}%"
    return f"{value:g}"


def describe_threshold(metric: MetricDef, value: float) -> str:
    """Threshold as it appears in rule copy: "20 tickets waiting", "45m", "100%"."""
    if metric.count_noun:
        singular, plural = metric.count_noun
        if value == 0:
            return f"no {plural}"
        return f"{value:g} {singular if value == 1 else plural}"
    return format_value(value, metric.unit)


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
