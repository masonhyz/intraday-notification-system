"""Ingest: idempotency, and surviving a feed we do not control."""

from __future__ import annotations

from .conftest import at, make_rule, snapshot


def test_a_redelivered_event_is_a_no_op(engine, store, people, channel):
    """Producers are at-least-once. The event log's primary key is what makes
    that safe - nothing downstream needs to think about it."""
    make_rule(store, metric="tickets_waiting", threshold=20)
    event = snapshot(tickets_waiting=25)

    first = engine.ingest(event)
    second = engine.ingest(dict(event))

    assert (first.status, second.status) == ("applied", "duplicate")
    assert len(channel.delivered) == 1, "a redelivery must not re-notify"
    assert store.events.count_by_status() == {"applied": 1}


def test_the_same_event_id_with_different_contents_is_still_a_duplicate(engine, store, people):
    """The sample feed does exactly this. The id is the identity."""
    make_rule(store, metric="tickets_waiting", threshold=20)
    engine.ingest(snapshot(event_id="evt_1", ts=at(0), tickets_waiting=25))

    result = engine.ingest(snapshot(event_id="evt_1", ts=at(9), tickets_waiting=1))

    assert result.status == "duplicate"
    assert store.state.get_queue("billing").tickets_waiting == 25


def test_a_late_event_is_logged_but_does_not_move_current_state(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20)
    engine.ingest(snapshot(ts=at(30), tickets_waiting=25))

    late = engine.ingest(snapshot(ts=at(10), tickets_waiting=0))

    assert late.status == "stale"
    assert store.state.get_queue("billing").tickets_waiting == 25
    assert store.events.count_by_status()["stale"] == 1
    assert len(channel.delivered) == 1, "a stale event must not resolve a live incident"


def test_an_unparseable_event_is_recorded_and_the_pipeline_keeps_going(engine, store, people):
    bad = {"event_id": "evt_bad", "ts": "2026-05-26T09:00:00Z", "type": "agent_state_change"}

    result = engine.ingest(bad)

    assert result.status == "rejected"
    assert "agent_id" in result.note
    assert store.events.count_by_status() == {"rejected": 1}


def test_an_unknown_event_type_is_kept_for_inspection(engine, store):
    result = engine.ingest(
        {"event_id": "evt_x", "ts": "2026-05-26T09:00:00Z", "type": "shift_swap"}
    )

    assert result.status == "rejected"
    assert "unknown event type" in result.note
    assert store.events.recent()[0]["type"] == "shift_swap"


def test_garbage_input_does_not_raise(engine, store):
    assert engine.ingest({}).status == "rejected"
    assert engine.ingest({"type": "queue_snapshot"}).status == "rejected"


def test_ingest_many_reports_per_event_status(engine, store, people):
    make_rule(store, metric="tickets_waiting", threshold=20)
    events = [snapshot(ts=at(0), tickets_waiting=25), {"event_id": "b", "type": "nope"}]

    statuses = [r.status for r in engine.ingest_many(events)]

    assert statuses == ["applied", "rejected"]
