"""State folding, with an emphasis on the ways a real feed misbehaves."""

from __future__ import annotations

from app.clock import iso
from app.models import SubjectType, parse_event
from app.state import apply_event

from .conftest import adherence, at, snapshot, state_change


def apply(raw, state=None):
    return apply_event(state, parse_event(raw).event, "org_demo")


def test_snapshot_fields_land_on_state():
    result = apply(snapshot(tickets_waiting=12, longest_wait_sec=300))
    assert result.applied
    assert result.state.tickets_waiting == 12
    assert result.state.longest_wait_sec == 300


def test_a_late_snapshot_does_not_roll_state_backwards():
    fresh = apply(snapshot(ts=at(30), tickets_waiting=3)).state
    late = apply(snapshot(ts=at(10), tickets_waiting=99), fresh)

    assert not late.applied
    assert late.note == "stale snapshot"
    assert late.state.tickets_waiting == 3


def test_missing_snapshot_fields_keep_the_last_known_value():
    """`null` means "not reported", which is not the same as zero."""
    first = apply(snapshot(volume_forecast_next_15m=40)).state
    second = apply(snapshot(ts=at(1), volume_forecast_next_15m=None), first).state

    assert second.volume_forecast_next_15m == 40


def test_null_queue_ids_keep_the_agent_on_their_queues():
    known = apply(state_change(queue_ids=["billing", "vip"])).state
    after = apply(state_change(ts=at(1), queue_ids=None, new_state="on_call"), known).state
    after = apply(adherence(ts=at(2), queue_ids=[], actual_state="on_call"), after).state

    assert after.queue_ids == ["billing", "vip"]


def test_state_since_only_resets_on_a_real_transition():
    s = apply(state_change(ts=at(0), new_state="on_call")).state
    s = apply(state_change(ts=at(5), new_state="on_call"), s).state

    assert iso(s.state_since) == iso(at(0))


def test_violation_without_a_start_time_falls_back_to_the_check_time():
    """An open violation is still a violation; under-count rather than drop it."""
    s = apply(
        adherence(ts=at(10), actual_state="in_meeting", in_violation=True,
                  violation_started_at=None)
    ).state

    assert s.in_violation
    assert iso(s.violation_started_at) == iso(at(10))


def test_leaving_violation_clears_the_start_time():
    s = apply(adherence(ts=at(1), actual_state="on_break", in_violation=True,
                        violation_started_at=at(0).isoformat().replace("+00:00", "Z"))).state
    s = apply(adherence(ts=at(2), actual_state="available", in_violation=False), s).state

    assert not s.in_violation
    assert s.violation_started_at is None


def test_adherence_check_reconciles_a_transition_we_never_saw():
    """Dropped events are normal. The periodic check is our repair mechanism."""
    s = apply(state_change(ts=at(0), new_state="available")).state
    result = apply(adherence(ts=at(9), actual_state="in_meeting", in_violation=True), s)

    assert result.state.state == "in_meeting"
    assert iso(result.state.state_since) == iso(at(9))
    assert "reconciled" in result.note


def test_a_late_adherence_check_is_dropped_without_blocking_state_changes():
    """The two streams carry separate watermarks.

    A late adherence check must not undo newer adherence data, and must not
    freeze the state-change stream, which tracks its own recency.
    """
    s = apply(state_change(ts=at(5), new_state="available")).state
    s = apply(adherence(ts=at(20), actual_state="available", in_violation=True), s).state
    assert s.in_violation

    late = apply(adherence(ts=at(10), actual_state="available", in_violation=False), s)
    assert not late.applied
    assert late.note == "stale adherence check"
    assert late.state.in_violation, "a late check must not clear a live violation"

    moved = apply(state_change(ts=at(12), new_state="on_call"), late.state)
    assert moved.applied
    assert moved.state.state == "on_call"


def test_state_knowledge_is_shared_across_streams():
    """An adherence check is evidence about the agent's state at its timestamp,
    so a state change older than that evidence is stale."""
    s = apply(state_change(ts=at(5), new_state="available")).state
    s = apply(adherence(ts=at(20), actual_state="in_meeting", in_violation=True), s).state

    older = apply(state_change(ts=at(15), new_state="on_call"), s)

    assert not older.applied
    assert older.state.state == "in_meeting"
    assert older.state.subject.type is SubjectType.AGENT
