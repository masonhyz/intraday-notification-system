"""Demo org: the people, and the rule book they would plausibly have set up.

This is fixture data, not part of the system. It exists so that `make demo`
shows a realistic configuration out of the box and so the end-to-end test has something
concrete to assert against.
"""

from __future__ import annotations

from .models import AudienceTarget, AudienceType, Role, Scope, ScopeMode, Severity, User
from .rules import validate
from .store import Store
from .templates import BY_KEY

AGENTS = {
    "a_05": "Sam Okafor",
    "a_07": "Joy Nakamura",
    "a_11": "Ravi Menon",
    "a_19": "Nina Alvarez",
    "a_23": "Tomas Berg",
    "a_31": "Iris Chen",
    "a_42": "Leo Fontaine",
    "a_88": "Mei Ling",
}


def seed(store: Store) -> None:
    """Idempotent: safe to run against an existing database."""
    _seed_users(store)
    if not store.rules.list():
        _seed_rules(store)


def _seed_users(store: Store) -> None:
    store.users.upsert(
        User(
            id="u_dana",
            org_id=store.org_id,
            name="Dana Whitfield",
            role=Role.TEAM_LEAD,
            queue_ids=["billing"],
            channel="slack",
        )
    )
    store.users.upsert(
        User(
            id="u_marco",
            org_id=store.org_id,
            name="Marco Reyes",
            role=Role.TEAM_LEAD,
            queue_ids=["tier_2", "vip"],
            channel="slack",
        )
    )
    store.users.upsert(
        User(
            id="u_priya",
            org_id=store.org_id,
            name="Priya Nair",
            role=Role.HEAD_OF_SUPPORT,
            queue_ids=["billing", "tier_2", "vip"],
            channel="email",
            # Interrupt only for genuine fires; everything else arrives as a
            # quarter-hourly roll-up.
            digest_min_severity=Severity.CRITICAL,
            digest_interval_sec=900,
        )
    )
    for agent_id, name in AGENTS.items():
        store.users.upsert(
            User(
                id=f"u_{agent_id}",
                org_id=store.org_id,
                name=name,
                role=Role.AGENT,
                agent_id=agent_id,
                channel="push",
            )
        )


def _seed_rules(store: Store) -> None:
    """A starter rule book, one rule per thing a real team would watch.

    Notice what is *not* here: an alert for every metric we can measure. Each
    rule has an owner who can act on it, and thresholds chosen so it stays
    quiet on a normal morning.
    """
    rules = [
        BY_KEY["sla_breached"].build(created_by="u_dana"),
        # Early warning only where the SLA is short enough that a breach alert
        # would arrive too late to do anything about: vip promises 60 seconds.
        BY_KEY["sla_at_risk"].build(
            name="SLA at risk (VIP)",
            scope=Scope(ScopeMode.IDS, ("vip",)),
            created_by="u_marco",
        ),
        BY_KEY["queue_backlog"].build(
            name="Billing backing up",
            scope=Scope(ScopeMode.IDS, ("billing",)),
            created_by="u_dana",
        ),
        BY_KEY["coverage_gap"].build(
            name="Nobody available on billing",
            scope=Scope(ScopeMode.IDS, ("billing",)),
            created_by="u_dana",
        ),
        BY_KEY["volume_spike"].build(created_by="u_priya"),
        BY_KEY["adherence_self"].build(created_by="u_dana"),
        BY_KEY["adherence_escalation"].build(created_by="u_dana"),
        BY_KEY["long_call"].build(created_by="u_dana"),
    ]
    for rule in rules:
        rule.org_id = store.org_id
        validate(rule)
        store.rules.upsert(rule)
