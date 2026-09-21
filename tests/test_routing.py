"""Audience resolution: one rule, the right people."""

from __future__ import annotations

from app.models import AudienceTarget, AudienceType, Role, Subject, SubjectType, User
from app.routing import resolve_audience

from .conftest import adherence, at, make_rule, snapshot, state_change


def recipients(store):
    """Who a notification was addressed to, whether it went out immediately or
    was held for a digest. Delivery timing is tested in test_digest.py."""
    return sorted(n.recipient_id for n in store.notifications.recent())


def test_queue_owners_receive_rules_about_their_queue(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20)

    engine.ingest(snapshot(queue_id="billing", tickets_waiting=25))

    assert recipients(store) == ["u_head", "u_lead"]


def test_queue_owners_of_other_queues_are_left_alone(engine, store, people, channel):
    make_rule(store, metric="tickets_waiting", threshold=20)

    engine.ingest(snapshot(queue_id="tier_2", tickets_waiting=25))

    assert "u_lead" not in recipients(store)
    assert "u_other" in recipients(store)


def test_an_agent_rule_reaches_the_leads_of_every_queue_the_agent_serves(
    engine, store, people, channel
):
    """Agents float between queues; ownership follows the agent's routing."""
    make_rule(
        store, metric="adherence_violation_sec", threshold=60,
        subject_type=SubjectType.AGENT,
    )
    engine.ingest(
        adherence(
            ts=at(5), queue_ids=["billing", "tier_2"], actual_state="on_break",
            in_violation=True, violation_started_at=at(0).isoformat().replace("+00:00", "Z"),
        )
    )

    assert recipients(store) == ["u_head", "u_lead", "u_other"]


def test_the_agent_involved_gets_their_own_nudge(engine, store, people, channel):
    """One rule serves every agent - nobody configures 800 of these."""
    make_rule(
        store, metric="adherence_violation_sec", threshold=60,
        subject_type=SubjectType.AGENT,
        audience=[AudienceTarget(AudienceType.SUBJECT_AGENT)],
    )
    engine.ingest(
        adherence(ts=at(5), actual_state="on_break", in_violation=True,
                  violation_started_at=at(0).isoformat().replace("+00:00", "Z"))
    )

    assert recipients(store) == ["u_a_19"]
    assert channel.delivered[0].channel == "push"


def test_an_agent_without_an_account_does_not_block_the_rest_of_the_audience(
    engine, store, people, channel
):
    make_rule(
        store, metric="adherence_violation_sec", threshold=60,
        subject_type=SubjectType.AGENT,
        audience=[
            AudienceTarget(AudienceType.SUBJECT_AGENT),
            AudienceTarget(AudienceType.QUEUE_LEADS),
        ],
    )
    engine.ingest(
        adherence(agent_id="a_unknown", ts=at(5), actual_state="on_break", in_violation=True,
                  violation_started_at=at(0).isoformat().replace("+00:00", "Z"))
    )

    assert recipients(store) == ["u_head", "u_lead"]


def test_a_person_matching_twice_is_only_notified_once(store, people):
    rule = make_rule(
        store, metric="tickets_waiting", threshold=20,
        audience=[
            AudienceTarget(AudienceType.QUEUE_LEADS),
            AudienceTarget(AudienceType.ROLE, "team_lead"),
            AudienceTarget(AudienceType.USER, "u_lead"),
        ],
    )
    state = store.state.get_queue("billing")

    resolved = resolve_audience(
        rule, Subject(SubjectType.QUEUE, "billing"), state, store.users
    )

    assert [u.id for u in resolved].count("u_lead") == 1


def test_role_audiences_pick_up_new_joiners(engine, store, people, channel):
    make_rule(
        store, metric="tickets_waiting", threshold=20,
        audience=[AudienceTarget(AudienceType.ROLE, "team_lead")],
    )
    store.users.upsert(
        User(id="u_new", org_id=store.org_id, name="Sam", role=Role.TEAM_LEAD, queue_ids=[])
    )

    engine.ingest(snapshot(tickets_waiting=25))

    assert "u_new" in recipients(store)


def test_a_named_user_is_notified_regardless_of_queue(engine, store, people, channel):
    make_rule(
        store, metric="tickets_waiting", threshold=20,
        audience=[AudienceTarget(AudienceType.USER, "u_other")],
    )

    engine.ingest(snapshot(queue_id="billing", tickets_waiting=25))

    assert recipients(store) == ["u_other"]
