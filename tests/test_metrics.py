"""Metric computation, including the "does not apply right now" cases."""

from __future__ import annotations

import pytest

from app.metrics import CATALOG, describe_threshold, format_duration, format_value, get_metric
from app.models import parse_event
from app.state import apply_event

from .conftest import adherence, at, snapshot, state_change


def queue_state(**fields):
    return apply_event(None, parse_event(snapshot(**fields)).event, "org_demo").state


def agent_state(*raws):
    state = None
    for raw in raws:
        state = apply_event(state, parse_event(raw).event, "org_demo").state
    return state


def value(key, state, now=None):
    return get_metric(key).compute(state, now or at(0))


def test_sla_ratio_normalises_across_queues_with_different_targets():
    fast = queue_state(longest_wait_sec=90, sla_target_sec=60)
    slow = queue_state(longest_wait_sec=90, sla_target_sec=300)

    assert value("sla_ratio", fast) == pytest.approx(1.5)
    assert value("sla_ratio", slow) == pytest.approx(0.3)


def test_sla_ratio_is_unavailable_without_a_target():
    assert value("sla_ratio", queue_state(sla_target_sec=None, longest_wait_sec=10)) is None


def test_volume_vs_forecast_is_unavailable_when_the_forecast_is_missing():
    """The feed really does send `volume_forecast_next_15m: null`."""
    assert value("volume_vs_forecast", queue_state(volume_forecast_next_15m=None)) is None
    assert value("volume_vs_forecast", queue_state(volume_forecast_next_15m=0)) is None
    assert value(
        "volume_vs_forecast", queue_state(volume_last_15m=30, volume_forecast_next_15m=20)
    ) == pytest.approx(1.5)


def test_time_in_state_counts_forward_from_the_transition():
    state = agent_state(state_change(ts=at(0), new_state="on_call"))

    assert value("time_in_state_sec", state, at(45)) == pytest.approx(45 * 60)


def test_adherence_violation_is_none_when_in_adherence():
    ok = agent_state(adherence(ts=at(0)))
    bad = agent_state(
        adherence(ts=at(10), actual_state="on_break", in_violation=True,
                  violation_started_at=at(2).isoformat().replace("+00:00", "Z"))
    )

    assert value("adherence_violation_sec", ok) is None
    assert value("adherence_violation_sec", bad, at(12)) == pytest.approx(10 * 60)


def test_every_metric_tolerates_an_empty_subject():
    """Rules must not explode on a subject we have barely heard from."""
    empty_queue = queue_state(
        tickets_waiting=None, longest_wait_sec=None, sla_target_sec=None,
        agents_available=None, agents_on_call=None, volume_last_15m=None,
        volume_forecast_next_15m=None,
    )
    empty_agent = agent_state(state_change(ts=at(0), new_state="available"))
    empty_agent.state_since = None

    for metric in CATALOG.values():
        state = empty_queue if metric.subject_type.value == "queue" else empty_agent
        assert metric.compute(state, at(0)) is None


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0s"), (45, "45s"), (60, "1m"), (150, "2m 30s"), (3600, "1h"), (3900, "1h 5m")],
)
def test_duration_formatting(seconds, expected):
    assert format_duration(seconds) == expected


def test_value_formatting_by_unit():
    assert format_value(1.0, "ratio") == "100%"
    assert format_value(None, "count") == "n/a"
    assert format_value(12, "count") == "12"


def test_count_thresholds_read_like_english():
    assert describe_threshold(get_metric("agents_available"), 1) == "1 agent available"
    assert describe_threshold(get_metric("agents_available"), 0) == "no agents available"
    assert describe_threshold(get_metric("tickets_waiting"), 20) == "20 tickets waiting"
