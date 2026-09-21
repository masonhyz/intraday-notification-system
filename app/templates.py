"""Rule templates: the starting points a user picks from.

Nobody opens a rule builder knowing that they want
`sla_ratio >= 1.0 sustained 120s, clear after 120s`. They know they want to
hear about SLA breaches. Templates carry the defaults that make a rule usable
on day one - especially the timing defaults, which are the difference between a
useful alert and a channel everyone mutes.

Templates are *presets*, not a second rule type: a template produces an
ordinary Rule which the user can then edit freely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .clock import utcnow
from .models import (
    AudienceTarget,
    AudienceType,
    Operator,
    Rule,
    Scope,
    ScopeMode,
    Severity,
    SubjectType,
)
from .store import new_id


@dataclass(frozen=True)
class RuleTemplate:
    key: str
    title: str
    blurb: str            # what it is for, in the words a user would use
    subject_type: SubjectType
    metric: str
    operator: Operator
    threshold: float
    severity: Severity
    audience: tuple[AudienceTarget, ...]
    state_filter: str | None = None
    for_sec: int = 0
    clear_after_sec: int = 0
    renotify_sec: int = 0
    notify_on_resolve: bool = True
    #: Free-form "who is this for", shown in the picker.
    for_whom: str = "team lead"

    def build(
        self,
        *,
        name: str | None = None,
        scope: Scope | None = None,
        audience: list[AudienceTarget] | None = None,
        created_by: str | None = None,
        **overrides: Any,
    ) -> Rule:
        now = utcnow()
        rule = Rule(
            id=new_id("rule"),
            org_id="",  # filled in by the store's org binding at write time
            name=name or self.title,
            subject_type=self.subject_type,
            metric=self.metric,
            operator=self.operator,
            threshold=self.threshold,
            severity=self.severity,
            audience=list(audience if audience is not None else self.audience),
            scope=scope or Scope(),
            state_filter=self.state_filter,
            for_sec=self.for_sec,
            clear_after_sec=self.clear_after_sec,
            renotify_sec=self.renotify_sec,
            notify_on_resolve=self.notify_on_resolve,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        for key, value in overrides.items():
            setattr(rule, key, value)
        return rule

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "blurb": self.blurb,
            "for_whom": self.for_whom,
            "subject_type": self.subject_type.value,
            "metric": self.metric,
            "operator": self.operator.value,
            "threshold": self.threshold,
            "severity": self.severity.value,
            "state_filter": self.state_filter,
            "for_sec": self.for_sec,
            "clear_after_sec": self.clear_after_sec,
            "renotify_sec": self.renotify_sec,
            "notify_on_resolve": self.notify_on_resolve,
            "audience": [a.to_dict() for a in self.audience],
        }


_LEADS = (AudienceTarget(AudienceType.QUEUE_LEADS),)
_LEADS_AND_HEAD = (
    AudienceTarget(AudienceType.QUEUE_LEADS),
    AudienceTarget(AudienceType.ROLE, "head_of_support"),
)
_SELF = (AudienceTarget(AudienceType.SUBJECT_AGENT),)


TEMPLATES: tuple[RuleTemplate, ...] = (
    RuleTemplate(
        key="sla_breached",
        title="SLA breached",
        blurb="The oldest ticket has passed the queue's promised answer time.",
        subject_type=SubjectType.QUEUE,
        metric="sla_ratio",
        operator=Operator.GTE,
        threshold=1.0,
        severity=Severity.CRITICAL,
        audience=_LEADS_AND_HEAD,
        for_sec=120,
        clear_after_sec=120,
        renotify_sec=900,
    ),
    RuleTemplate(
        key="sla_at_risk",
        title="SLA at risk",
        blurb="The oldest ticket has burned most of the SLA but has not missed it yet.",
        subject_type=SubjectType.QUEUE,
        metric="sla_ratio",
        operator=Operator.GTE,
        threshold=0.8,
        severity=Severity.WARNING,
        audience=_LEADS,
        for_sec=60,
        clear_after_sec=120,
    ),
    RuleTemplate(
        key="queue_backlog",
        title="Queue backing up",
        blurb="More tickets waiting than the team can work through.",
        subject_type=SubjectType.QUEUE,
        metric="tickets_waiting",
        operator=Operator.GTE,
        threshold=20,
        severity=Severity.WARNING,
        audience=_LEADS_AND_HEAD,
        for_sec=120,
        clear_after_sec=300,
    ),
    RuleTemplate(
        key="coverage_gap",
        title="Nobody available",
        blurb="A queue has run out of agents who can pick up work.",
        subject_type=SubjectType.QUEUE,
        metric="agents_available",
        operator=Operator.LTE,
        threshold=0,
        severity=Severity.WARNING,
        audience=_LEADS,
        for_sec=600,
        # One agent freeing up for a few minutes is not the end of a coverage
        # gap. A short clear window turns a single staffing problem into three
        # separate alerts over an hour, which is how leads learn to mute you.
        clear_after_sec=900,
    ),
    RuleTemplate(
        key="volume_spike",
        title="Volume above forecast",
        blurb="Contacts are arriving faster than the forecast expected.",
        subject_type=SubjectType.QUEUE,
        metric="volume_vs_forecast",
        operator=Operator.GTE,
        threshold=1.3,
        severity=Severity.WARNING,
        audience=(AudienceTarget(AudienceType.ROLE, "head_of_support"),),
        for_sec=600,
        clear_after_sec=600,
        for_whom="head of support",
    ),
    RuleTemplate(
        key="adherence_self",
        title="Heads-up: you are out of adherence",
        blurb="Nudge the agent themselves so they can get back on track.",
        subject_type=SubjectType.AGENT,
        metric="adherence_violation_sec",
        operator=Operator.GTE,
        threshold=600,
        severity=Severity.INFO,
        audience=_SELF,
        for_whom="agent",
    ),
    RuleTemplate(
        key="adherence_escalation",
        title="Agent out of adherence for a long time",
        blurb="The agent has not corrected it on their own; the lead should step in.",
        subject_type=SubjectType.AGENT,
        metric="adherence_violation_sec",
        operator=Operator.GTE,
        threshold=1800,
        severity=Severity.WARNING,
        audience=_LEADS,
        notify_on_resolve=False,
    ),
    RuleTemplate(
        key="long_call",
        title="Call running long",
        blurb="An agent has been on one call long enough that they may be stuck.",
        subject_type=SubjectType.AGENT,
        metric="time_in_state_sec",
        operator=Operator.GTE,
        threshold=2700,
        severity=Severity.WARNING,
        audience=_LEADS,
        state_filter="on_call",
        notify_on_resolve=False,
    ),
    RuleTemplate(
        key="long_break",
        title="Break running long",
        blurb="An agent has been on break longer than the scheduled break.",
        subject_type=SubjectType.AGENT,
        metric="time_in_state_sec",
        operator=Operator.GTE,
        threshold=900,
        severity=Severity.INFO,
        audience=_SELF,
        state_filter="on_break",
        for_whom="agent",
    ),
)

BY_KEY = {t.key: t for t in TEMPLATES}
