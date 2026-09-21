"""Shared fixtures.

Every test drives time explicitly through a ManualClock. Nothing sleeps, and
nothing depends on how long the test takes to run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.clock import ManualClock
from app.db import connect
from app.engine import Engine
from app.models import (
    AudienceTarget,
    AudienceType,
    Operator,
    Role,
    Rule,
    Scope,
    ScopeMode,
    Severity,
    SubjectType,
    User,
)
from app.notify import CollectingChannel, Notifier
from app.store import Store, new_id

T0 = datetime(2026, 5, 26, 9, 0, 0, tzinfo=timezone.utc)


def at(minutes: float = 0, seconds: float = 0) -> datetime:
    return T0 + timedelta(minutes=minutes, seconds=seconds)


@pytest.fixture
def store() -> Store:
    s = Store(connect(":memory:"))
    yield s
    s.close()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def channel() -> CollectingChannel:
    return CollectingChannel()


@pytest.fixture
def engine(store: Store, clock: ManualClock, channel: CollectingChannel) -> Engine:
    return Engine(store, clock=clock, notifier=Notifier(store, [channel]))


@pytest.fixture
def people(store: Store) -> dict[str, User]:
    """One lead for billing, one agent, one head of support who only wants fires."""
    users = {
        "lead": User(
            id="u_lead", org_id=store.org_id, name="Dana", role=Role.TEAM_LEAD,
            queue_ids=["billing"],
        ),
        "other_lead": User(
            id="u_other", org_id=store.org_id, name="Marco", role=Role.TEAM_LEAD,
            queue_ids=["tier_2"],
        ),
        "agent": User(
            id="u_a_19", org_id=store.org_id, name="Nina", role=Role.AGENT,
            agent_id="a_19", channel="push",
        ),
        "head": User(
            id="u_head", org_id=store.org_id, name="Priya", role=Role.HEAD_OF_SUPPORT,
            queue_ids=["billing", "tier_2"], channel="email",
            digest_min_severity=Severity.CRITICAL, digest_interval_sec=900,
        ),
    }
    for user in users.values():
        store.users.upsert(user)
    return users


def make_rule(
    store: Store,
    *,
    metric: str,
    threshold: float,
    subject_type: SubjectType = SubjectType.QUEUE,
    operator: Operator = Operator.GTE,
    audience: list[AudienceTarget] | None = None,
    name: str = "Test rule",
    **kwargs,
) -> Rule:
    rule = Rule(
        id=new_id("rule"),
        org_id=store.org_id,
        name=name,
        subject_type=subject_type,
        metric=metric,
        operator=operator,
        threshold=threshold,
        audience=audience or [AudienceTarget(AudienceType.QUEUE_LEADS)],
        created_at=T0,
        updated_at=T0,
        **kwargs,
    )
    store.rules.upsert(rule)
    return rule


def snapshot(queue_id: str = "billing", ts: datetime | None = None, **fields) -> dict:
    payload = {
        "event_id": new_id("evt"),
        "ts": (ts or T0).isoformat().replace("+00:00", "Z"),
        "type": "queue_snapshot",
        "queue_id": queue_id,
        "tickets_waiting": 0,
        "longest_wait_sec": 0,
        "sla_target_sec": 120,
        "agents_available": 3,
        "agents_on_call": 1,
        "volume_last_15m": 10,
        "volume_forecast_next_15m": 10,
    }
    payload.update(fields)
    return payload


def state_change(agent_id: str = "a_19", ts: datetime | None = None, **fields) -> dict:
    payload = {
        "event_id": new_id("evt"),
        "ts": (ts or T0).isoformat().replace("+00:00", "Z"),
        "type": "agent_state_change",
        "agent_id": agent_id,
        "queue_ids": ["billing"],
        "previous_state": None,
        "previous_state_duration_sec": None,
        "new_state": "available",
    }
    payload.update(fields)
    return payload


def adherence(agent_id: str = "a_19", ts: datetime | None = None, **fields) -> dict:
    payload = {
        "event_id": new_id("evt"),
        "ts": (ts or T0).isoformat().replace("+00:00", "Z"),
        "type": "adherence_check",
        "agent_id": agent_id,
        "queue_ids": ["billing"],
        "scheduled_state": "available",
        "actual_state": "available",
        "in_violation": False,
        "violation_started_at": None,
    }
    payload.update(fields)
    return payload
