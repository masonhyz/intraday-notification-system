"""Digest delivery: the recipient-level noise control.

A head of support is on the same rules as their leads. What differs is how much
of it is allowed to interrupt them.
"""

from __future__ import annotations

from app.models import Operator, Severity

from .conftest import at, make_rule, snapshot


def held(store):
    return store.notifications.buffered()


def test_critical_alerts_interrupt_immediately(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, severity=Severity.CRITICAL)

    engine.ingest(snapshot(tickets_waiting=25))

    assert sorted(n.recipient_id for n in channel.delivered) == ["u_head", "u_lead"]
    assert held(store) == []


def test_anything_quieter_is_held_back_for_the_head_of_support(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, severity=Severity.WARNING)

    engine.ingest(snapshot(tickets_waiting=25))

    assert [n.recipient_id for n in channel.delivered] == ["u_lead"]
    assert [n.recipient_id for n in held(store)] == ["u_head"]


def test_nothing_is_sent_before_the_digest_interval_is_up(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, severity=Severity.WARNING)
    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))

    engine.tick(at(10))

    assert [n.kind for n in channel.delivered] == ["fire"]  # the lead's copy only


def test_the_digest_goes_out_one_interval_after_the_first_held_alert(
    engine, store, people, channel
):
    make_rule(store, metric="tickets_waiting", threshold=20, severity=Severity.WARNING)
    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))

    engine.tick(at(16))

    digests = [n for n in channel.delivered if n.kind == "digest"]
    assert len(digests) == 1
    assert digests[0].recipient_id == "u_head"
    assert held(store) == [], "everything in the digest is marked as rolled up"


def test_a_digest_separates_what_is_still_open_from_what_sorted_itself_out(
    engine, store, people, channel
):
    """Telling a head of support to go look at something that recovered twenty
    minutes ago is worse than saying nothing."""
    make_rule(store, metric="tickets_waiting", threshold=20, severity=Severity.WARNING,
              name="Backlog")
    make_rule(store, metric="agents_available", threshold=0, operator=Operator.LTE,
              severity=Severity.WARNING, name="No coverage")

    engine.ingest(snapshot(ts=at(0), tickets_waiting=25, agents_available=0))
    engine.ingest(snapshot(ts=at(5), tickets_waiting=25, agents_available=4))
    engine.tick(at(16))

    digest = next(n for n in channel.delivered if n.kind == "digest")
    assert "Still needing attention:" in digest.body
    assert "Flared and recovered on their own:" in digest.body
    assert digest.title == "Digest: 1 open, 1 recovered"
    assert "lasted 5m" in digest.body


def test_reminders_collapse_into_one_digest_line(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, severity=Severity.WARNING,
              renotify_sec=300, name="Backlog")

    for minute in range(0, 16):
        engine.ingest(snapshot(ts=at(minute), tickets_waiting=25))
    engine.tick(at(16))

    digest = next(n for n in channel.delivered if n.kind == "digest")
    assert digest.body.count("Backlog") == 1
    assert digest.title == "Digest: 1 open, 0 recovered"


def test_a_forced_flush_empties_the_buffer_at_the_end_of_a_run(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, severity=Severity.WARNING)
    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))

    engine.notifier.flush_digests(at(2), force=True)

    assert held(store) == []
    assert any(n.kind == "digest" for n in channel.delivered)
