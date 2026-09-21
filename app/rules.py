"""Rule semantics: validation, and rendering a rule as a plain-English sentence.

The sentence is not decoration. A rule people cannot read is a rule people do
not trust, so the same `describe()` powers the live preview in the rule
builder, the rule list, and the "why am I getting this?" line on every
notification.
"""

from __future__ import annotations

from .metrics import CATALOG, describe_threshold, format_duration, get_metric
from .models import (
    AudienceTarget,
    AudienceType,
    Operator,
    Role,
    Rule,
    ScopeMode,
    SubjectType,
)

_OPERATOR_WORDS = {
    Operator.GTE: "at least",
    Operator.GT: "more than",
    Operator.LTE: "at most",
    Operator.LT: "under",
    Operator.EQ: "exactly",
}

_ROLE_WORDS = {
    Role.AGENT: "every agent",
    Role.TEAM_LEAD: "every team lead",
    Role.HEAD_OF_SUPPORT: "the head of support",
}


class RuleValidationError(ValueError):
    """Raised with a message meant to be shown to the person editing the rule."""


def validate(rule: Rule) -> None:
    if not rule.name.strip():
        raise RuleValidationError("Give the rule a name.")

    metric = CATALOG.get(rule.metric)
    if metric is None:
        raise RuleValidationError(
            f"Unknown metric {rule.metric!r}. Available: {', '.join(sorted(CATALOG))}."
        )
    if metric.subject_type is not rule.subject_type:
        raise RuleValidationError(
            f"Metric {metric.key!r} describes a {metric.subject_type.value}, "
            f"but this rule is about a {rule.subject_type.value}."
        )
    if rule.state_filter and not metric.supports_state_filter:
        raise RuleValidationError(f"Metric {metric.key!r} cannot be filtered by agent state.")

    for field in ("for_sec", "clear_after_sec", "renotify_sec"):
        if getattr(rule, field) < 0:
            raise RuleValidationError(f"{field} cannot be negative.")
    if rule.renotify_sec and rule.renotify_sec < 60:
        raise RuleValidationError("Reminders cannot repeat more often than once a minute.")

    if rule.scope.mode is ScopeMode.QUEUES and rule.subject_type is not SubjectType.AGENT:
        raise RuleValidationError("Scoping by queue only applies to rules about agents.")
    if rule.scope.mode is not ScopeMode.ALL and not rule.scope.ids:
        raise RuleValidationError("Pick at least one queue or agent, or scope the rule to all.")

    if not rule.audience:
        raise RuleValidationError("A rule with nobody to notify will never do anything.")
    for target in rule.audience:
        if target.type is AudienceType.SUBJECT_AGENT and rule.subject_type is not SubjectType.AGENT:
            raise RuleValidationError(
                "'The agent involved' can only receive rules that are about an agent."
            )
        if target.type in (AudienceType.USER, AudienceType.ROLE) and not target.value:
            raise RuleValidationError(f"Audience of type {target.type.value} needs a value.")
        if target.type is AudienceType.ROLE:
            try:
                Role(target.value)
            except ValueError:
                raise RuleValidationError(f"Unknown role {target.value!r}.") from None


def describe_condition(rule: Rule) -> str:
    """e.g. "has burned at least 100% of its SLA target"."""
    metric = get_metric(rule.metric)
    template = metric.phrase
    if rule.state_filter and metric.phrase_with_state:
        template = metric.phrase_with_state
    return template.format(
        op=_OPERATOR_WORDS[rule.operator],
        value=describe_threshold(metric, rule.threshold),
        state=(rule.state_filter or "").replace("_", " "),
    ).replace(" at most no ", " no ").replace(" exactly no ", " no ")


def describe_threshold_phrase(rule: Rule) -> str:
    """Just the threshold side, e.g. "at least 100%" - used in notification copy."""
    metric = get_metric(rule.metric)
    phrase = f"{_OPERATOR_WORDS[rule.operator]} {describe_threshold(metric, rule.threshold)}"
    return phrase.replace("at most no ", "no ").replace("exactly no ", "no ")


def describe_scope(rule: Rule) -> str:
    noun = "queue" if rule.subject_type is SubjectType.QUEUE else "agent"
    if rule.scope.mode is ScopeMode.ALL:
        return f"any {noun}"
    if rule.scope.mode is ScopeMode.QUEUES:
        return f"any agent on {_join(rule.scope.ids, 'or')}"
    return _join(rule.scope.ids, "or")


def describe_audience(rule: Rule, names: dict[str, str] | None = None) -> str:
    names = names or {}
    parts = [_audience_phrase(t, names) for t in rule.audience]
    return _join(parts)


def _audience_phrase(target: AudienceTarget, names: dict[str, str]) -> str:
    if target.type is AudienceType.USER:
        return names.get(target.value or "", target.value or "someone")
    if target.type is AudienceType.ROLE:
        try:
            return _ROLE_WORDS[Role(target.value)]
        except ValueError:  # pragma: no cover - guarded by validate()
            return str(target.value)
    if target.type is AudienceType.SUBJECT_AGENT:
        return "the agent involved"
    return "whoever is responsible for the queue"


def describe(rule: Rule, names: dict[str, str] | None = None) -> str:
    """The rule as a sentence, e.g.

    "When any queue has burned at least 100% of its SLA target, sustained for
    2m, notify whoever is responsible for the queue. Repeats every 15m; also
    notifies when it recovers."
    """
    sentence = f"When {describe_scope(rule)} {describe_condition(rule)}"
    if rule.for_sec:
        sentence += f", sustained for {format_duration(rule.for_sec)}"
    sentence += f", notify {describe_audience(rule, names)}."

    tail = []
    if rule.renotify_sec:
        tail.append(f"Repeats every {format_duration(rule.renotify_sec)}")
    if rule.notify_on_resolve:
        tail.append("also notifies when it recovers")
    if tail:
        sentence += " " + "; ".join(tail).capitalize() + "."
    return sentence


def _join(items, conjunction: str = "and") -> str:
    items = [str(i) for i in items]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" {conjunction} " + items[-1]
