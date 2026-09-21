"""Persistence. Thin repositories over SQLite, one per table family.

Everything the engine needs is here so the engine itself has no SQL in it and
can be tested against an in-memory database. Each repository is bound to a
single org: multi-tenancy plumbing is out of scope, but the key is threaded
through so it never has to be retrofitted.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Iterable, Sequence

from .clock import iso, parse_ts
from .models import (
    AudienceTarget,
    Notification,
    Operator,
    Role,
    Rule,
    Scope,
    Severity,
    Subject,
    SubjectType,
    User,
)
from .state import AgentState, EntityState, QueueState


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class _Repo:
    def __init__(self, conn: sqlite3.Connection, org_id: str) -> None:
        self.conn = conn
        self.org_id = org_id


# --------------------------------------------------------------------------


class EventRepo(_Repo):
    def record(
        self,
        *,
        event_id: str,
        ts: datetime | None,
        received_at: datetime,
        type_: str,
        subject: Subject | None,
        status: str,
        note: str | None,
        payload: dict[str, Any],
    ) -> bool:
        """Append to the event log. Returns False if we have seen this id before.

        The PRIMARY KEY on event_id is the idempotency boundary for the whole
        system: producers may redeliver, and a redelivered event is a no-op.
        """
        try:
            self.conn.execute(
                "INSERT INTO events (event_id, org_id, ts, received_at, type, subject_type,"
                " subject_id, status, note, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    self.org_id,
                    iso(ts) or iso(received_at),
                    iso(received_at),
                    type_,
                    subject.type.value if subject else None,
                    subject.id if subject else None,
                    status,
                    note,
                    json.dumps(payload),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def count_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM events WHERE org_id = ? GROUP BY status",
            (self.org_id,),
        )
        return {r["status"]: r["n"] for r in rows}

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE org_id = ? ORDER BY received_at DESC, rowid DESC LIMIT ?",
            (self.org_id, limit),
        )
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------


class StateRepo(_Repo):
    def get_queue(self, queue_id: str) -> QueueState | None:
        row = self.conn.execute(
            "SELECT * FROM queue_state WHERE org_id = ? AND queue_id = ?",
            (self.org_id, queue_id),
        ).fetchone()
        return _row_to_queue(row) if row else None

    def get_agent(self, agent_id: str) -> AgentState | None:
        row = self.conn.execute(
            "SELECT * FROM agent_state WHERE org_id = ? AND agent_id = ?",
            (self.org_id, agent_id),
        ).fetchone()
        return _row_to_agent(row) if row else None

    def get(self, subject: Subject) -> EntityState | None:
        if subject.type is SubjectType.QUEUE:
            return self.get_queue(subject.id)
        return self.get_agent(subject.id)

    def save(self, state: EntityState) -> None:
        if isinstance(state, QueueState):
            self.conn.execute(
                "INSERT INTO queue_state (org_id, queue_id, updated_at, tickets_waiting,"
                " longest_wait_sec, sla_target_sec, agents_available, agents_on_call,"
                " volume_last_15m, volume_forecast_next_15m) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, queue_id) DO UPDATE SET"
                " updated_at=excluded.updated_at, tickets_waiting=excluded.tickets_waiting,"
                " longest_wait_sec=excluded.longest_wait_sec, sla_target_sec=excluded.sla_target_sec,"
                " agents_available=excluded.agents_available, agents_on_call=excluded.agents_on_call,"
                " volume_last_15m=excluded.volume_last_15m,"
                " volume_forecast_next_15m=excluded.volume_forecast_next_15m",
                (
                    state.org_id,
                    state.queue_id,
                    iso(state.updated_at),
                    state.tickets_waiting,
                    state.longest_wait_sec,
                    state.sla_target_sec,
                    state.agents_available,
                    state.agents_on_call,
                    state.volume_last_15m,
                    state.volume_forecast_next_15m,
                ),
            )
        else:
            self.conn.execute(
                "INSERT INTO agent_state (org_id, agent_id, updated_at, state, state_since,"
                " state_updated_at, queue_ids, scheduled_state, actual_state, in_violation,"
                " violation_started_at, adherence_updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, agent_id) DO UPDATE SET"
                " updated_at=excluded.updated_at, state=excluded.state,"
                " state_since=excluded.state_since, state_updated_at=excluded.state_updated_at,"
                " queue_ids=excluded.queue_ids, scheduled_state=excluded.scheduled_state,"
                " actual_state=excluded.actual_state, in_violation=excluded.in_violation,"
                " violation_started_at=excluded.violation_started_at,"
                " adherence_updated_at=excluded.adherence_updated_at",
                (
                    state.org_id,
                    state.agent_id,
                    iso(state.updated_at),
                    state.state,
                    iso(state.state_since),
                    iso(state.state_updated_at),
                    json.dumps(state.queue_ids),
                    state.scheduled_state,
                    state.actual_state,
                    int(state.in_violation),
                    iso(state.violation_started_at),
                    iso(state.adherence_updated_at),
                ),
            )

    def all_queues(self) -> list[QueueState]:
        rows = self.conn.execute(
            "SELECT * FROM queue_state WHERE org_id = ? ORDER BY queue_id", (self.org_id,)
        )
        return [_row_to_queue(r) for r in rows]

    def all_agents(self) -> list[AgentState]:
        rows = self.conn.execute(
            "SELECT * FROM agent_state WHERE org_id = ? ORDER BY agent_id", (self.org_id,)
        )
        return [_row_to_agent(r) for r in rows]

    def all_of(self, subject_type: SubjectType) -> list[EntityState]:
        return self.all_queues() if subject_type is SubjectType.QUEUE else self.all_agents()


def _row_to_queue(row: sqlite3.Row) -> QueueState:
    return QueueState(
        org_id=row["org_id"],
        queue_id=row["queue_id"],
        updated_at=parse_ts(row["updated_at"]),
        tickets_waiting=row["tickets_waiting"],
        longest_wait_sec=row["longest_wait_sec"],
        sla_target_sec=row["sla_target_sec"],
        agents_available=row["agents_available"],
        agents_on_call=row["agents_on_call"],
        volume_last_15m=row["volume_last_15m"],
        volume_forecast_next_15m=row["volume_forecast_next_15m"],
    )


def _row_to_agent(row: sqlite3.Row) -> AgentState:
    return AgentState(
        org_id=row["org_id"],
        agent_id=row["agent_id"],
        updated_at=parse_ts(row["updated_at"]),
        state=row["state"],
        state_since=parse_ts(row["state_since"]),
        state_updated_at=parse_ts(row["state_updated_at"]),
        queue_ids=json.loads(row["queue_ids"]),
        scheduled_state=row["scheduled_state"],
        actual_state=row["actual_state"],
        in_violation=bool(row["in_violation"]),
        violation_started_at=parse_ts(row["violation_started_at"]),
        adherence_updated_at=parse_ts(row["adherence_updated_at"]),
    )


# --------------------------------------------------------------------------


class RuleRepo(_Repo):
    def list(self, *, subject_type: SubjectType | None = None, enabled_only: bool = False) -> list[Rule]:
        sql = "SELECT * FROM rules WHERE org_id = ?"
        params: list[Any] = [self.org_id]
        if subject_type is not None:
            sql += " AND subject_type = ?"
            params.append(subject_type.value)
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY created_at, id"
        return [_row_to_rule(r) for r in self.conn.execute(sql, params)]

    def get(self, rule_id: str) -> Rule | None:
        row = self.conn.execute(
            "SELECT * FROM rules WHERE org_id = ? AND id = ?", (self.org_id, rule_id)
        ).fetchone()
        return _row_to_rule(row) if row else None

    def upsert(self, rule: Rule) -> Rule:
        self.conn.execute(
            "INSERT INTO rules (id, org_id, name, enabled, subject_type, scope, metric, operator,"
            " threshold, state_filter, for_sec, clear_after_sec, renotify_sec, notify_on_resolve,"
            " severity, audience, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, enabled=excluded.enabled,"
            " subject_type=excluded.subject_type, scope=excluded.scope, metric=excluded.metric,"
            " operator=excluded.operator, threshold=excluded.threshold,"
            " state_filter=excluded.state_filter, for_sec=excluded.for_sec,"
            " clear_after_sec=excluded.clear_after_sec, renotify_sec=excluded.renotify_sec,"
            " notify_on_resolve=excluded.notify_on_resolve, severity=excluded.severity,"
            " audience=excluded.audience, updated_at=excluded.updated_at",
            (
                rule.id,
                self.org_id,
                rule.name,
                int(rule.enabled),
                rule.subject_type.value,
                rule.scope.to_json(),
                rule.metric,
                rule.operator.value,
                float(rule.threshold),
                rule.state_filter,
                rule.for_sec,
                rule.clear_after_sec,
                rule.renotify_sec,
                int(rule.notify_on_resolve),
                rule.severity.value,
                json.dumps([a.to_dict() for a in rule.audience]),
                rule.created_by,
                iso(rule.created_at),
                iso(rule.updated_at),
            ),
        )
        return rule

    def delete(self, rule_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM rules WHERE org_id = ? AND id = ?", (self.org_id, rule_id)
        )
        self.conn.execute("DELETE FROM rule_subject_state WHERE org_id = ? AND rule_id = ?",
                          (self.org_id, rule_id))
        return cur.rowcount > 0


def _row_to_rule(row: sqlite3.Row) -> Rule:
    return Rule(
        id=row["id"],
        org_id=row["org_id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        subject_type=SubjectType(row["subject_type"]),
        scope=Scope.from_json(row["scope"]),
        metric=row["metric"],
        operator=Operator(row["operator"]),
        threshold=row["threshold"],
        state_filter=row["state_filter"],
        for_sec=row["for_sec"],
        clear_after_sec=row["clear_after_sec"],
        renotify_sec=row["renotify_sec"],
        notify_on_resolve=bool(row["notify_on_resolve"]),
        severity=Severity(row["severity"]),
        audience=[AudienceTarget.from_dict(a) for a in json.loads(row["audience"])],
        created_by=row["created_by"],
        created_at=parse_ts(row["created_at"]),
        updated_at=parse_ts(row["updated_at"]),
    )


# --------------------------------------------------------------------------


class RuleSubjectState:
    """Mutable engine state for one (rule, subject) pair."""

    __slots__ = (
        "rule_id", "org_id", "subject_id", "status", "condition_since", "clear_since",
        "incident_id", "opened_at", "last_value", "last_eval_at", "last_notified_at", "notify_seq",
    )

    def __init__(self, rule_id: str, org_id: str, subject_id: str, status: str = "ok") -> None:
        self.rule_id = rule_id
        self.org_id = org_id
        self.subject_id = subject_id
        self.status = status
        self.condition_since: datetime | None = None
        self.clear_since: datetime | None = None
        self.incident_id: str | None = None
        self.opened_at: datetime | None = None
        self.last_value: float | None = None
        self.last_eval_at: datetime | None = None
        self.last_notified_at: datetime | None = None
        self.notify_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}


class EngineStateRepo(_Repo):
    def get(self, rule_id: str, subject_id: str) -> RuleSubjectState | None:
        row = self.conn.execute(
            "SELECT * FROM rule_subject_state WHERE rule_id = ? AND subject_id = ?",
            (rule_id, subject_id),
        ).fetchone()
        if not row:
            return None
        s = RuleSubjectState(row["rule_id"], row["org_id"], row["subject_id"], row["status"])
        s.condition_since = parse_ts(row["condition_since"])
        s.clear_since = parse_ts(row["clear_since"])
        s.incident_id = row["incident_id"]
        s.opened_at = parse_ts(row["opened_at"])
        s.last_value = row["last_value"]
        s.last_eval_at = parse_ts(row["last_eval_at"])
        s.last_notified_at = parse_ts(row["last_notified_at"])
        s.notify_seq = row["notify_seq"]
        return s

    def save(self, s: RuleSubjectState) -> None:
        self.conn.execute(
            "INSERT INTO rule_subject_state (rule_id, org_id, subject_id, status, condition_since,"
            " clear_since, incident_id, opened_at, last_value, last_eval_at, last_notified_at,"
            " notify_seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(rule_id, subject_id) DO UPDATE SET status=excluded.status,"
            " condition_since=excluded.condition_since, clear_since=excluded.clear_since,"
            " incident_id=excluded.incident_id, opened_at=excluded.opened_at,"
            " last_value=excluded.last_value, last_eval_at=excluded.last_eval_at,"
            " last_notified_at=excluded.last_notified_at, notify_seq=excluded.notify_seq",
            (
                s.rule_id, s.org_id, s.subject_id, s.status, iso(s.condition_since),
                iso(s.clear_since), s.incident_id, iso(s.opened_at), s.last_value,
                iso(s.last_eval_at), iso(s.last_notified_at), s.notify_seq,
            ),
        )

    def open_incidents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT r.name AS rule_name, r.severity, r.subject_type, s.* FROM rule_subject_state s"
            " JOIN rules r ON r.id = s.rule_id"
            " WHERE s.org_id = ? AND s.status IN ('firing','clearing')"
            " ORDER BY s.opened_at",
            (self.org_id,),
        )
        return [dict(r) for r in rows]

    def active_pairs(self) -> list[tuple[str, str]]:
        """(rule_id, subject_id) pairs mid-flight, which a tick must revisit."""
        rows = self.conn.execute(
            "SELECT rule_id, subject_id FROM rule_subject_state"
            " WHERE org_id = ? AND status != 'ok'",
            (self.org_id,),
        )
        return [(r["rule_id"], r["subject_id"]) for r in rows]


# --------------------------------------------------------------------------


class UserRepo(_Repo):
    def list(self) -> list[User]:
        rows = self.conn.execute(
            "SELECT * FROM users WHERE org_id = ? ORDER BY role, name", (self.org_id,)
        )
        return [_row_to_user(r) for r in rows]

    def get(self, user_id: str) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE org_id = ? AND id = ?", (self.org_id, user_id)
        ).fetchone()
        return _row_to_user(row) if row else None

    def by_agent_id(self, agent_id: str) -> User | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE org_id = ? AND agent_id = ?", (self.org_id, agent_id)
        ).fetchone()
        return _row_to_user(row) if row else None

    def by_role(self, role: Role) -> list[User]:
        rows = self.conn.execute(
            "SELECT * FROM users WHERE org_id = ? AND role = ? ORDER BY name",
            (self.org_id, role.value),
        )
        return [_row_to_user(r) for r in rows]

    def responsible_for_queues(self, queue_ids: Iterable[str]) -> list[User]:
        wanted = set(queue_ids)
        if not wanted:
            return []
        return [u for u in self.list() if wanted & set(u.queue_ids)]

    def upsert(self, user: User) -> User:
        self.conn.execute(
            "INSERT INTO users (id, org_id, name, role, agent_id, queue_ids, channel,"
            " digest_min_severity, digest_interval_sec) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, role=excluded.role,"
            " agent_id=excluded.agent_id, queue_ids=excluded.queue_ids, channel=excluded.channel,"
            " digest_min_severity=excluded.digest_min_severity,"
            " digest_interval_sec=excluded.digest_interval_sec",
            (
                user.id, self.org_id, user.name, user.role.value, user.agent_id,
                json.dumps(user.queue_ids), user.channel, user.digest_min_severity.value,
                user.digest_interval_sec,
            ),
        )
        return user


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        org_id=row["org_id"],
        name=row["name"],
        role=Role(row["role"]),
        agent_id=row["agent_id"],
        queue_ids=json.loads(row["queue_ids"]),
        channel=row["channel"],
        digest_min_severity=Severity(row["digest_min_severity"]),
        digest_interval_sec=row["digest_interval_sec"],
    )


# --------------------------------------------------------------------------


class NotificationRepo(_Repo):
    def insert(self, n: Notification) -> bool:
        """Persist a notification. False means the dedupe key already existed."""
        try:
            self.conn.execute(
                "INSERT INTO notifications (id, org_id, dedupe_key, created_at, rule_id, rule_name,"
                " incident_id, kind, severity, subject_type, subject_id, recipient_id, channel,"
                " title, body, context, delivery, status, delivered_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    n.id, self.org_id, n.dedupe_key, iso(n.created_at), n.rule_id, n.rule_name,
                    n.incident_id, n.kind, n.severity.value,
                    n.subject_type.value if n.subject_type else None, n.subject_id,
                    n.recipient_id, n.channel, n.title, n.body, json.dumps(n.context),
                    n.delivery, n.status, iso(n.delivered_at),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def recent(
        self,
        *,
        limit: int = 100,
        recipient_id: str | None = None,
        include_buffered: bool = True,
    ) -> list[Notification]:
        sql = "SELECT * FROM notifications WHERE org_id = ?"
        params: list[Any] = [self.org_id]
        if recipient_id:
            sql += " AND recipient_id = ?"
            params.append(recipient_id)
        if not include_buffered:
            sql += " AND status = 'delivered'"
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        return [_row_to_notification(r) for r in self.conn.execute(sql, params)]

    def buffered(self, recipient_id: str | None = None) -> list[Notification]:
        sql = "SELECT * FROM notifications WHERE org_id = ? AND status = 'buffered'"
        params: list[Any] = [self.org_id]
        if recipient_id:
            sql += " AND recipient_id = ?"
            params.append(recipient_id)
        sql += " ORDER BY created_at"
        return [_row_to_notification(r) for r in self.conn.execute(sql, params)]

    def mark_rolled_up(self, ids: Sequence[str], at: datetime) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(
            f"UPDATE notifications SET status='rolled_up', delivered_at=?"
            f" WHERE org_id = ? AND id IN ({placeholders})",
            (iso(at), self.org_id, *ids),
        )

    def count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE org_id = ?", (self.org_id,)
        ).fetchone()[0]


def _row_to_notification(row: sqlite3.Row) -> Notification:
    return Notification(
        id=row["id"],
        org_id=row["org_id"],
        dedupe_key=row["dedupe_key"],
        created_at=parse_ts(row["created_at"]),
        kind=row["kind"],
        severity=Severity(row["severity"]),
        recipient_id=row["recipient_id"],
        channel=row["channel"],
        title=row["title"],
        body=row["body"],
        delivery=row["delivery"],
        status=row["status"],
        rule_id=row["rule_id"],
        rule_name=row["rule_name"],
        incident_id=row["incident_id"],
        subject_type=SubjectType(row["subject_type"]) if row["subject_type"] else None,
        subject_id=row["subject_id"],
        context=json.loads(row["context"]),
        delivered_at=parse_ts(row["delivered_at"]),
    )


# --------------------------------------------------------------------------


class Store:
    """Facade holding one connection and the repositories bound to an org."""

    def __init__(self, conn: sqlite3.Connection, org_id: str = "org_demo") -> None:
        self.conn = conn
        self.org_id = org_id
        self.events = EventRepo(conn, org_id)
        self.state = StateRepo(conn, org_id)
        self.rules = RuleRepo(conn, org_id)
        self.engine_state = EngineStateRepo(conn, org_id)
        self.users = UserRepo(conn, org_id)
        self.notifications = NotificationRepo(conn, org_id)

    def close(self) -> None:
        self.conn.close()
