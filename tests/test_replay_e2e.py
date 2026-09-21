"""End-to-end over the sample feed.

This is the test that says the system does the job: 96 raw events - including
a redelivery, an out-of-order snapshot and two events with fields missing -
become a small number of notifications addressed to the people who can act on
them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.clock import ManualClock, iso, parse_ts
from app.db import connect
from app.engine import Engine
from app.notify import CollectingChannel, Notifier
from app.replay import read_events, replay
from app.seed import seed
from app.store import Store

FEED = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"


@pytest.fixture(scope="module")
def replayed():
    events = list(read_events(FEED))
    store = Store(connect(":memory:"))
    seed(store)
    channel = CollectingChannel()
    engine = Engine(
        store,
        clock=ManualClock(parse_ts(events[0]["ts"])),
        notifier=Notifier(store, [channel]),
    )
    counts = replay(engine, events, tick_sec=30, trailing_sec=600)
    yield store, channel, counts
    store.close()


def sent(store, *, recipient=None, rule=None):
    return [
        n
        for n in store.notifications.recent(limit=500)[::-1]
        if (recipient is None or n.recipient_id == recipient)
        and (rule is None or n.rule_name == rule)
    ]


# -- ingest accounting -----------------------------------------------------


def test_the_planted_data_problems_are_classified_not_crashed(replayed):
    _, _, counts = replayed

    assert counts["applied"] == 94
    assert counts["duplicate"] == 1, "evt_01HXYZ050 appears twice with different contents"
    assert counts["stale"] == 1, "the 09:49 vip snapshot arrives after the 10:30 one"
    assert counts["rejected"] == 0


def test_the_out_of_order_snapshot_did_not_rewind_the_queue(replayed):
    """The vip snapshot for 09:49 arrives last in the file. Current state must
    still be the 10:30 picture."""
    store, _, _ = replayed
    vip = store.state.get_queue("vip")

    assert iso(vip.updated_at) == "2026-05-26T10:30:00.000Z"
    assert (vip.tickets_waiting, vip.agents_available) == (0, 2)


# -- the shape of what gets sent -------------------------------------------


def test_a_45_minute_sla_breach_is_four_messages_not_ninety(replayed):
    """billing sits over its SLA target from 09:30 to 10:15, across a dozen
    snapshots and ninety evaluation ticks. Its lead hears about it once, is
    reminded twice, and is told when it is over."""
    store, _, _ = replayed
    dana = sent(store, recipient="u_dana", rule="SLA breached")

    assert [n.kind for n in dana] == ["fire", "reminder", "reminder", "resolve"]
    assert len({n.incident_id for n in dana}) == 1, "one incident, start to finish"
    assert iso(dana[0].created_at).startswith("2026-05-26T09:32")
    assert iso(dana[-1].created_at).startswith("2026-05-26T10:17")


def test_each_queue_breach_reaches_only_its_own_lead(replayed):
    store, _, _ = replayed

    billing = {n.recipient_id for n in sent(store, rule="SLA breached") if n.subject_id == "billing"}
    tier_2 = {n.recipient_id for n in sent(store, rule="SLA breached") if n.subject_id == "tier_2"}

    assert billing == {"u_dana", "u_priya"}
    assert tier_2 == {"u_marco", "u_priya"}


def test_the_agent_who_drifted_gets_one_nudge_and_one_all_clear(replayed):
    """a_19 goes on break at 09:35 while scheduled available, and comes back at
    10:10. One heads-up ten minutes in, one all-clear."""
    store, _, _ = replayed
    nina = sent(store, recipient="u_a_19")

    assert [n.kind for n in nina] == ["fire", "resolve"]
    assert iso(nina[0].created_at).startswith("2026-05-26T09:45")
    assert nina[0].channel == "push"


def test_agents_only_ever_hear_about_themselves(replayed):
    store, _, _ = replayed

    for user in store.users.list():
        if user.role.value != "agent":
            continue
        assert {n.subject_id for n in sent(store, recipient=user.id)} <= {user.agent_id}


def test_the_lead_is_only_pulled_in_once_the_agent_has_not_fixed_it(replayed):
    """Two thresholds on one metric: a nudge to the agent at 10 minutes, an
    escalation to the lead at 30."""
    store, _, _ = replayed
    escalations = sent(store, rule="Agent out of adherence for a long time")

    assert {n.subject_id for n in escalations} == {"a_19", "a_88"}
    assert all(n.recipient_id in {"u_dana", "u_marco", "u_priya"} for n in escalations)


def test_a_violation_with_no_start_time_still_reaches_the_agent(replayed):
    """a_23's 10:15 check says `in_violation: true` with a null start. Falling
    back to the check time is what turns that into a real notification."""
    store, _, _ = replayed

    assert [n.subject_id for n in sent(store, recipient="u_a_23")] == ["a_23"]


def test_long_calls_are_caught_while_they_are_still_running(replayed):
    """No `agent_state_change` ever reports these: a_11, a_07, a_31 and a_42
    are still on the call when the alert fires."""
    store, _, _ = replayed

    assert {n.subject_id for n in sent(store, rule="Call running long")} == {
        "a_11", "a_07", "a_31", "a_42"
    }


# -- who is interrupted ----------------------------------------------------


def test_the_head_of_support_is_only_interrupted_by_fires(replayed):
    store, channel, _ = replayed
    pushed = [n for n in channel.delivered if n.recipient_id == "u_priya"]

    assert {n.severity.value for n in pushed if n.kind != "digest"} == {"critical"}
    assert any(n.kind == "digest" for n in pushed), "the rest arrives as a roll-up"


def test_everything_held_back_eventually_arrives_in_a_digest(replayed):
    store, _, _ = replayed

    assert store.notifications.buffered() == [], "nothing is left stranded in the buffer"
    assert all(
        n.status in ("delivered", "rolled_up")
        for n in store.notifications.recent(limit=500)
    )


def test_nothing_fires_for_rules_whose_conditions_never_held(replayed):
    """Volume stayed under forecast all morning, and vip never got close to its
    SLA. Configured-but-quiet is the normal state of a rule."""
    store, _, _ = replayed

    assert sent(store, rule="Volume above forecast") == []
    assert sent(store, rule="SLA at risk (VIP)") == []


def test_total_volume_stays_in_the_range_a_person_can_read(replayed):
    """A change detector. If a change to the engine moves this number, the diff
    should say why - a busy 90 minutes across 11 people should not produce
    hundreds of messages."""
    store, _, _ = replayed

    assert store.notifications.count() == 44
