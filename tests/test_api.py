"""The HTTP surface: rule configuration, the inbox, and ingest."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import ENV_CONSOLE, ENV_DB, ENV_TICK, app
from app.clock import iso, utcnow


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_DB, str(tmp_path / "test.db"))
    monkeypatch.setenv(ENV_TICK, "0")      # tests drive evaluation explicitly
    monkeypatch.setenv(ENV_CONSOLE, "0")
    with TestClient(app) as c:
        yield c


def a_rule(**overrides):
    payload = {
        "name": "Billing backlog",
        "subject_type": "queue",
        "metric": "tickets_waiting",
        "operator": ">=",
        "threshold": 20,
        "severity": "warning",
        "audience": [{"type": "queue_leads"}],
        "scope_mode": "ids",
        "scope_ids": ["billing"],
        "for_sec": 120,
    }
    payload.update(overrides)
    return payload


def snapshot(**overrides):
    payload = {
        "event_id": "evt_api_1",
        # A live producer sends current timestamps; the engine ignores state it
        # has not heard about recently (see Engine.staleness_sec).
        "ts": iso(utcnow()),
        "type": "queue_snapshot",
        "queue_id": "billing",
        "tickets_waiting": 30,
        "longest_wait_sec": 30,
        "sla_target_sec": 120,
        "agents_available": 2,
        "agents_on_call": 1,
        "volume_last_15m": 10,
        "volume_forecast_next_15m": 10,
    }
    payload.update(overrides)
    return payload


def test_the_ui_is_served(client):
    res = client.get("/")

    assert res.status_code == 200
    assert "Intraday notifications" in res.text


def test_bootstrap_describes_everything_the_builder_needs(client):
    data = client.get("/api/bootstrap").json()

    assert {m["key"] for m in data["metrics"]} >= {"sla_ratio", "adherence_violation_sec"}
    assert data["templates"], "the rule builder starts from templates"
    assert {u["role"] for u in data["users"]} == {"agent", "team_lead", "head_of_support"}


def test_a_new_rule_comes_back_with_its_plain_english_description(client):
    res = client.post("/api/rules", json=a_rule())

    assert res.status_code == 201
    assert res.json()["description"] == (
        "When billing has at least 20 tickets waiting, sustained for 2m, notify whoever "
        "is responsible for the queue. Also notifies when it recovers."
    )


def test_an_impossible_rule_is_rejected_with_a_message_a_person_can_act_on(client):
    res = client.post(
        "/api/rules",
        json=a_rule(audience=[{"type": "subject_agent"}]),
    )

    assert res.status_code == 400
    assert "can only receive rules that are about an agent" in res.json()["detail"]


def test_a_metric_that_does_not_belong_to_the_subject_is_rejected(client):
    res = client.post("/api/rules", json=a_rule(metric="adherence_violation_sec"))

    assert res.status_code == 400
    assert "describes a agent" in res.json()["detail"]


def test_preview_explains_a_draft_without_saving_it(client):
    before = len(client.get("/api/rules").json())

    res = client.post("/api/rules/preview", json=a_rule(threshold=50))

    assert res.json()["ok"] is True
    assert "at least 50 tickets waiting" in res.json()["description"]
    assert len(client.get("/api/rules").json()) == before


def test_preview_reports_why_a_draft_is_invalid(client):
    res = client.post("/api/rules/preview", json=a_rule(scope_mode="ids", scope_ids=[]))

    assert res.json() == {
        "ok": False,
        "error": "Pick at least one queue or agent, or scope the rule to all.",
    }


def test_a_rule_can_be_edited_in_place(client):
    rule_id = client.post("/api/rules", json=a_rule()).json()["id"]

    res = client.put(f"/api/rules/{rule_id}", json=a_rule(threshold=40, name="Renamed"))

    assert res.status_code == 200
    assert res.json()["threshold"] == 40
    assert res.json()["name"] == "Renamed"


def test_a_rule_can_be_switched_off_without_being_deleted(client):
    rule_id = client.post("/api/rules", json=a_rule()).json()["id"]

    client.post(f"/api/rules/{rule_id}/enabled", json={"enabled": False})

    assert client.get("/api/rules").json()[-1]["enabled"] is False


def test_deleting_a_rule_is_idempotent_from_the_callers_point_of_view(client):
    rule_id = client.post("/api/rules", json=a_rule()).json()["id"]

    assert client.delete(f"/api/rules/{rule_id}").status_code == 204
    assert client.delete(f"/api/rules/{rule_id}").status_code == 404


def test_posting_events_produces_notifications_in_the_inbox(client):
    client.post("/api/rules", json=a_rule(for_sec=0))

    res = client.post("/api/events", json=[snapshot()])

    assert res.json()["counts"] == {"applied": 1}
    inbox = client.get("/api/notifications?recipient=u_dana").json()
    assert [n["rule_name"] for n in inbox] == ["Billing backlog"]
    assert inbox[0]["severity"] == "warning"
    assert inbox[0]["subject"] == "queue:billing"


def test_redelivering_an_event_over_http_changes_nothing(client):
    client.post("/api/rules", json=a_rule(for_sec=0))
    client.post("/api/events", json=snapshot())
    before = client.get("/api/notifications").json()

    res = client.post("/api/events", json=snapshot())

    assert res.json()["counts"] == {"duplicate": 1}
    assert client.get("/api/notifications").json() == before


def test_state_reports_current_queues_and_open_incidents(client):
    client.post("/api/rules", json=a_rule(for_sec=0))
    client.post("/api/events", json=snapshot())

    state = client.get("/api/state").json()

    assert state["queues"][0]["queue_id"] == "billing"
    assert state["queues"][0]["sla_display"] == "25%"
    assert [i["rule_name"] for i in state["incidents"]] == ["Billing backlog"]


def test_a_tick_can_be_forced_for_a_scripted_demo(client):
    assert client.post("/api/tick").json()["notifications"] == 0
