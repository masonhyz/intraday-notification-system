"""Rule validation. Every message here is shown to the person editing a rule,
so they are asserted verbatim."""

from __future__ import annotations

import pytest

from app.models import (
    AudienceTarget,
    AudienceType,
    Operator,
    Rule,
    Scope,
    ScopeMode,
    SubjectType,
)
from app.rules import RuleValidationError, describe, validate

from .conftest import T0


def rule(**overrides) -> Rule:
    payload = dict(
        id="rule_1",
        org_id="org_demo",
        name="Backlog",
        subject_type=SubjectType.QUEUE,
        metric="tickets_waiting",
        operator=Operator.GTE,
        threshold=20,
        audience=[AudienceTarget(AudienceType.QUEUE_LEADS)],
        created_at=T0,
        updated_at=T0,
    )
    payload.update(overrides)
    return Rule(**payload)


def message(**overrides) -> str:
    with pytest.raises(RuleValidationError) as exc:
        validate(rule(**overrides))
    return str(exc.value)


def test_a_valid_rule_passes():
    validate(rule())


def test_a_rule_needs_a_name():
    assert message(name="   ") == "Give the rule a name."


def test_an_unknown_metric_lists_the_ones_that_exist():
    text = message(metric="vibes")

    assert text.startswith("Unknown metric 'vibes'.")
    assert "sla_ratio" in text


def test_a_state_filter_only_applies_to_metrics_that_support_one():
    assert message(state_filter="on_call") == (
        "Metric 'tickets_waiting' cannot be filtered by agent state."
    )


def test_negative_timings_are_rejected():
    assert message(for_sec=-1) == "for_sec cannot be negative."


def test_reminders_cannot_be_set_to_spam():
    assert message(renotify_sec=30) == (
        "Reminders cannot repeat more often than once a minute."
    )


def test_queue_scoping_is_meaningless_for_a_queue_rule():
    assert message(scope=Scope(ScopeMode.QUEUES, ("billing",))) == (
        "Scoping by queue only applies to rules about agents."
    )


def test_a_narrowed_scope_needs_something_in_it():
    assert message(scope=Scope(ScopeMode.IDS, ())) == (
        "Pick at least one queue or agent, or scope the rule to all."
    )


def test_a_rule_with_nobody_to_notify_is_rejected():
    assert message(audience=[]) == (
        "A rule with nobody to notify will never do anything."
    )


def test_an_unknown_role_is_rejected():
    assert message(audience=[AudienceTarget(AudienceType.ROLE, "cfo")]) == (
        "Unknown role 'cfo'."
    )


def test_a_role_audience_needs_a_role():
    assert message(audience=[AudienceTarget(AudienceType.ROLE, None)]) == (
        "Audience of type role needs a value."
    )


def test_descriptions_name_people_rather_than_ids():
    described = describe(
        rule(audience=[AudienceTarget(AudienceType.USER, "u_dana")]),
        {"u_dana": "Dana Whitfield"},
    )

    assert "Dana Whitfield" in described
    assert "u_dana" not in described
