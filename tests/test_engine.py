"""The (rule, subject) incident state machine.

These are the tests that decide whether the system is usable: everything here
is about *not* sending something.
"""

from __future__ import annotations

from app.engine import Engine
from app.models import AudienceTarget, AudienceType, Operator, SubjectType
from app.store import new_id

from .conftest import adherence, at, make_rule, snapshot, state_change


def titles(channel):
    return [n.title for n in channel.delivered]


def kinds(channel):
    return [n.kind for n in channel.delivered]


def alerts(channel):
    """Kinds of the per-incident messages, ignoring periodic digests."""
    return [n.kind for n in channel.delivered if n.kind != "digest"]


# -- firing ----------------------------------------------------------------


def test_fires_as_soon_as_the_condition_is_true_when_there_is_no_sustain_window(
    engine, store, people, channel
):
    make_rule(store, metric="tickets_waiting", threshold=20, name="Backlog")

    engine.ingest(snapshot(tickets_waiting=25))

    assert kinds(channel) == ["fire"]
    assert "Backlog — billing" in titles(channel)[0]


def test_a_brief_spike_inside_the_sustain_window_never_fires(engine, store, people, channel):
    """The whole point of `for_sec`: a queue that crosses the line for one
    snapshot is not an incident."""
    make_rule(store, metric="tickets_waiting", threshold=20, for_sec=300, name="Backlog")

    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))
    engine.ingest(snapshot(ts=at(2), tickets_waiting=25))
    engine.ingest(snapshot(ts=at(4), tickets_waiting=3))
    engine.tick(at(30))

    assert channel.delivered == []


def test_fires_once_the_condition_has_held_for_the_whole_window(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, for_sec=300, name="Backlog")

    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))
    engine.tick(at(4))
    assert channel.delivered == []

    engine.tick(at(5))
    assert kinds(channel) == ["fire"]


def test_an_open_incident_does_not_re_fire_on_every_snapshot(engine, store, people, channel):
    """Queue snapshots arrive every 30 seconds. One incident, one notification."""
    make_rule(store, metric="tickets_waiting", threshold=20, name="Backlog")

    for minute in range(10):
        engine.ingest(snapshot(ts=at(minute), tickets_waiting=25))

    assert kinds(channel) == ["fire"]


def test_reminders_repeat_only_on_the_renotify_interval(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, renotify_sec=600, name="Backlog")

    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))
    for minute in range(1, 26):
        engine.ingest(snapshot(ts=at(minute), tickets_waiting=25))

    assert kinds(channel) == ["fire", "reminder", "reminder"]


# -- resolving -------------------------------------------------------------


def test_recovery_notifies_and_closes_the_incident(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, name="Backlog")

    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))
    engine.ingest(snapshot(ts=at(5), tickets_waiting=2))

    assert kinds(channel) == ["fire", "resolve"]
    assert titles(channel)[1].startswith("Recovered:")
    assert store.engine_state.open_incidents() == []


def test_notify_on_resolve_off_stays_quiet_on_recovery(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, notify_on_resolve=False)

    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))
    engine.ingest(snapshot(ts=at(5), tickets_waiting=2))

    assert kinds(channel) == ["fire"]


def test_flapping_around_the_threshold_produces_one_incident(engine, store, people, channel):
    """Without `clear_after_sec` this is four notifications. With it, one."""
    make_rule(store, metric="tickets_waiting", threshold=20, clear_after_sec=300)

    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))
    engine.ingest(snapshot(ts=at(1), tickets_waiting=19))
    engine.ingest(snapshot(ts=at(2), tickets_waiting=21))
    engine.ingest(snapshot(ts=at(3), tickets_waiting=18))
    engine.ingest(snapshot(ts=at(4), tickets_waiting=22))

    assert kinds(channel) == ["fire"]


def test_recovery_is_only_declared_after_it_has_held(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, clear_after_sec=300)

    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))
    engine.ingest(snapshot(ts=at(1), tickets_waiting=2))
    engine.tick(at(4))
    assert kinds(channel) == ["fire"]

    engine.tick(at(6))
    assert kinds(channel) == ["fire", "resolve"]


def test_a_second_incident_after_recovery_notifies_again(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20)

    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))
    engine.ingest(snapshot(ts=at(5), tickets_waiting=2))
    engine.ingest(snapshot(ts=at(10), tickets_waiting=30))

    assert kinds(channel) == ["fire", "resolve", "fire"]
    incidents = {n.incident_id for n in channel.delivered}
    assert len(incidents) == 2, "the second outage is a new incident"


# -- unavailable metrics ---------------------------------------------------


def test_a_metric_that_does_not_apply_is_not_a_breach(engine, store, people, channel):
    """`None` means "cannot say", which must never be read as zero and fire a
    `<=` rule."""
    make_rule(store, metric="volume_vs_forecast", threshold=0.5, operator=Operator.LTE)

    engine.ingest(snapshot(volume_last_15m=10, volume_forecast_next_15m=None))

    assert channel.delivered == []


# -- time-driven rules -----------------------------------------------------


def test_a_long_call_fires_during_the_call_not_after_it(engine, store, people, channel):
    """`agent_state_change` only reports a duration once the call *ends*. A lead
    who wants to rescue a stuck agent needs to hear about it while it is
    happening, which only a tick can do."""
    make_rule(
        store, metric="time_in_state_sec", threshold=45 * 60, subject_type=SubjectType.AGENT,
        state_filter="on_call", name="Long call",
    )
    engine.ingest(state_change(ts=at(0), new_state="on_call"))

    engine.tick(at(44))
    assert channel.delivered == []

    engine.tick(at(46))
    assert kinds(channel) == ["fire"]


def test_the_state_filter_stops_the_clock_when_the_agent_changes_state(
    engine, store, people, channel
):
    make_rule(
        store, metric="time_in_state_sec", threshold=45 * 60, subject_type=SubjectType.AGENT,
        state_filter="on_call", notify_on_resolve=False,
    )
    engine.ingest(state_change(ts=at(0), new_state="on_call"))
    engine.ingest(state_change(ts=at(40), new_state="available", previous_state="on_call"))

    engine.tick(at(90))

    assert channel.delivered == []


def test_adherence_violation_grows_with_the_clock(engine, store, people, channel):
    make_rule(
        store, metric="adherence_violation_sec", threshold=600,
        subject_type=SubjectType.AGENT, name="Out of adherence",
        audience=[AudienceTarget(AudienceType.SUBJECT_AGENT)],
    )
    engine.ingest(
        adherence(ts=at(0), actual_state="on_break", in_violation=True,
                  violation_started_at=at(0).isoformat().replace("+00:00", "Z"))
    )

    engine.tick(at(9))
    assert channel.delivered == []

    engine.tick(at(11))
    assert [n.recipient_id for n in channel.delivered] == ["u_a_19"]


# -- scope -----------------------------------------------------------------


def test_a_rule_scoped_to_one_queue_ignores_the_others(engine, store, people, channel):
    from app.models import Scope, ScopeMode

    make_rule(store, metric="tickets_waiting", threshold=20,
              scope=Scope(ScopeMode.IDS, ("billing",)))

    engine.ingest(snapshot(queue_id="tier_2", tickets_waiting=99))
    assert channel.delivered == []

    engine.ingest(snapshot(queue_id="billing", tickets_waiting=25))
    assert len(channel.delivered) == 1


def test_an_agent_rule_can_be_scoped_by_the_queues_the_agent_serves(
    engine, store, people, channel
):
    from app.models import Scope, ScopeMode

    make_rule(
        store, metric="adherence_violation_sec", threshold=60, subject_type=SubjectType.AGENT,
        scope=Scope(ScopeMode.QUEUES, ("billing",)),
        audience=[AudienceTarget(AudienceType.SUBJECT_AGENT)],
    )
    violation = dict(
        actual_state="on_break", in_violation=True,
        violation_started_at=at(0).isoformat().replace("+00:00", "Z"),
    )

    engine.ingest(adherence(agent_id="a_77", ts=at(5), queue_ids=["tier_2"], **violation))
    engine.ingest(adherence(agent_id="a_19", ts=at(5), queue_ids=["billing"], **violation))

    assert [n.subject_id for n in channel.delivered] == ["a_19"]


def test_a_disabled_rule_evaluates_nothing(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, enabled=False)

    engine.ingest(snapshot(tickets_waiting=99))

    assert channel.delivered == []


# -- stale subjects --------------------------------------------------------


def test_a_queue_that_stops_reporting_stops_producing_alerts(engine, store, people, channel):
    """Snapshots arrive every 30 seconds. Once they stop, the numbers are
    unknown - and a frozen picture must not be alerted on."""
    make_rule(store, metric="tickets_waiting", threshold=20, for_sec=600, name="Backlog")
    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))

    engine.tick(at(11))
    assert alerts(channel) == ["fire"], "11 minutes old is still trustworthy"

    make_rule(store, metric="tickets_waiting", threshold=10, name="Second rule")
    engine.tick(at(40))

    assert alerts(channel) == ["fire"], "the queue has been silent too long to judge"


def test_an_agent_timer_outlives_a_queue_snapshot(engine, store, people, channel):
    """An agent's state is a fact that persists, not a sample that expires, so
    a long call still fires even though no event arrived during it."""
    make_rule(
        store, metric="time_in_state_sec", threshold=45 * 60, subject_type=SubjectType.AGENT,
        state_filter="on_call",
    )
    engine.ingest(state_change(ts=at(0), new_state="on_call"))

    engine.tick(at(46))

    assert kinds(channel) == ["fire"]


def test_silence_does_not_count_as_recovery(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20, name="Backlog")
    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))

    engine.tick(at(120))

    assert alerts(channel) == ["fire"], "the incident stays open rather than auto-resolving"
    assert [i["status"] for i in store.engine_state.open_incidents()] == ["firing"]


def test_the_staleness_guard_can_be_turned_off(store, clock, channel, people):
    from app.notify import Notifier

    engine = Engine(store, clock=clock, notifier=Notifier(store, [channel]), staleness_sec={})
    make_rule(store, metric="tickets_waiting", threshold=20, for_sec=600, name="Backlog")
    engine.ingest(snapshot(ts=at(0), tickets_waiting=25))

    engine.tick(at(600))

    assert alerts(channel) == ["fire"]
