"""HTTP API and the web UI it backs.

Three surfaces, in order of importance:

* `/api/rules` - rule configuration. This is the product.
* `/api/notifications` - the inbox, filterable by recipient, which is how you
  see that the right things reached the right people.
* `/api/events` - ingest, for a real producer. Replay uses the same engine.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .clock import iso, utcnow
from .db import DEFAULT_DB_PATH, connect
from .engine import Engine
from .metrics import CATALOG, format_value
from .models import (
    AudienceTarget,
    AudienceType,
    Operator,
    Rule,
    Scope,
    ScopeMode,
    Severity,
    SubjectType,
)
from .notify import ConsoleChannel
from .rules import RuleValidationError, describe, validate
from .seed import seed
from .state import AgentState, QueueState
from .store import Store, new_id
from .templates import TEMPLATES

STATIC_DIR = Path(__file__).with_name("static")

#: Read at startup rather than import time so tests (and `INTRADAY_DB=... uvicorn`)
#: can point the server at a different database.
ENV_DB = "INTRADAY_DB"
ENV_TICK = "INTRADAY_TICK_SECONDS"   # 0 disables the background ticker
ENV_CONSOLE = "INTRADAY_CONSOLE"     # 0 silences the console channel


def build_engine(db_path: Path | str | None = None, *, console: bool = True) -> Engine:
    store = Store(connect(db_path or os.environ.get(ENV_DB, DEFAULT_DB_PATH)))
    seed(store)
    return Engine(store, channels=[ConsoleChannel()] if console else [])


@asynccontextmanager
async def lifespan(app: FastAPI):
    tick_seconds = int(os.environ.get(ENV_TICK, "15"))
    app.state.engine = build_engine(console=os.environ.get(ENV_CONSOLE, "1") != "0")
    ticker = asyncio.create_task(_ticker(app, tick_seconds)) if tick_seconds > 0 else None
    try:
        yield
    finally:
        if ticker is not None:
            ticker.cancel()
        app.state.engine.store.close()


async def _ticker(app: FastAPI, seconds: int) -> None:
    """Time-based rules need a heartbeat, not just incoming events."""
    while True:
        await asyncio.sleep(seconds)
        try:
            await asyncio.to_thread(app.state.engine.tick)
        except Exception as exc:  # pragma: no cover - keep the loop alive
            print(f"tick failed: {exc}")


app = FastAPI(title="Intraday notifications", lifespan=lifespan)


def engine_of() -> Engine:
    return app.state.engine


# --------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------


class AudienceIn(BaseModel):
    type: AudienceType
    value: str | None = None


class RuleIn(BaseModel):
    name: str
    subject_type: SubjectType
    metric: str
    operator: Operator = Operator.GTE
    threshold: float
    severity: Severity = Severity.WARNING
    audience: list[AudienceIn] = Field(min_length=1)
    scope_mode: ScopeMode = ScopeMode.ALL
    scope_ids: list[str] = []
    state_filter: str | None = None
    for_sec: int = 0
    clear_after_sec: int = 0
    renotify_sec: int = 0
    notify_on_resolve: bool = True
    enabled: bool = True
    created_by: str | None = None

    def to_rule(self, org_id: str, rule_id: str | None = None, created_at=None) -> Rule:
        now = utcnow()
        return Rule(
            id=rule_id or new_id("rule"),
            org_id=org_id,
            name=self.name.strip(),
            subject_type=self.subject_type,
            metric=self.metric,
            operator=self.operator,
            threshold=self.threshold,
            severity=self.severity,
            audience=[AudienceTarget(a.type, a.value) for a in self.audience],
            scope=Scope(self.scope_mode, tuple(self.scope_ids)),
            state_filter=self.state_filter or None,
            for_sec=self.for_sec,
            clear_after_sec=self.clear_after_sec,
            renotify_sec=self.renotify_sec,
            notify_on_resolve=self.notify_on_resolve,
            enabled=self.enabled,
            created_by=self.created_by,
            created_at=created_at or now,
            updated_at=now,
        )


def rule_json(rule: Rule, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.enabled,
        "subject_type": rule.subject_type.value,
        "metric": rule.metric,
        "operator": rule.operator.value,
        "threshold": rule.threshold,
        "severity": rule.severity.value,
        "scope_mode": rule.scope.mode.value,
        "scope_ids": list(rule.scope.ids),
        "state_filter": rule.state_filter,
        "for_sec": rule.for_sec,
        "clear_after_sec": rule.clear_after_sec,
        "renotify_sec": rule.renotify_sec,
        "notify_on_resolve": rule.notify_on_resolve,
        "audience": [a.to_dict() for a in rule.audience],
        "created_by": rule.created_by,
        "description": describe(rule, names),
    }


def notification_json(n) -> dict[str, Any]:
    return {
        "id": n.id,
        "created_at": iso(n.created_at),
        "kind": n.kind,
        "severity": n.severity.value,
        "recipient_id": n.recipient_id,
        "channel": n.channel,
        "title": n.title,
        "body": n.body,
        "delivery": n.delivery,
        "status": n.status,
        "rule_id": n.rule_id,
        "rule_name": n.rule_name,
        "incident_id": n.incident_id,
        "subject": f"{n.subject_type.value}:{n.subject_id}" if n.subject_type else None,
        "context": n.context,
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    """Everything the UI needs to render its pickers in one round trip."""
    store = engine_of().store
    return {
        "users": [
            {
                "id": u.id,
                "name": u.name,
                "role": u.role.value,
                "agent_id": u.agent_id,
                "queue_ids": u.queue_ids,
                "channel": u.channel,
                "digest_min_severity": u.digest_min_severity.value,
            }
            for u in store.users.list()
        ],
        "metrics": [m.to_dict() for m in CATALOG.values()],
        "templates": [t.to_dict() for t in TEMPLATES],
        "operators": [o.value for o in Operator],
        "severities": [s.value for s in Severity],
        "audience_types": [a.value for a in AudienceType],
        "queues": [q.queue_id for q in store.state.all_queues()],
        "agents": [a.agent_id for a in store.state.all_agents()],
        "agent_states": sorted(
            {a.state for a in store.state.all_agents() if a.state}
            | {"available", "on_call", "on_break", "in_meeting", "offline"}
        ),
    }


@app.get("/api/rules")
def list_rules() -> list[dict[str, Any]]:
    store = engine_of().store
    names = {u.id: u.name for u in store.users.list()}
    return [rule_json(r, names) for r in store.rules.list()]


@app.post("/api/rules", status_code=201)
def create_rule(payload: RuleIn) -> dict[str, Any]:
    store = engine_of().store
    rule = payload.to_rule(store.org_id)
    _validate(rule)
    store.rules.upsert(rule)
    names = {u.id: u.name for u in store.users.list()}
    return rule_json(rule, names)


@app.put("/api/rules/{rule_id}")
def update_rule(rule_id: str, payload: RuleIn) -> dict[str, Any]:
    store = engine_of().store
    existing = store.rules.get(rule_id)
    if existing is None:
        raise HTTPException(404, "rule not found")
    rule = payload.to_rule(store.org_id, rule_id=rule_id, created_at=existing.created_at)
    _validate(rule)
    store.rules.upsert(rule)
    names = {u.id: u.name for u in store.users.list()}
    return rule_json(rule, names)


@app.post("/api/rules/{rule_id}/enabled")
def set_enabled(rule_id: str, enabled: bool = Body(embed=True)) -> dict[str, Any]:
    store = engine_of().store
    rule = store.rules.get(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    rule.enabled = enabled
    rule.updated_at = utcnow()
    store.rules.upsert(rule)
    names = {u.id: u.name for u in store.users.list()}
    return rule_json(rule, names)


@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str) -> None:
    if not engine_of().store.rules.delete(rule_id):
        raise HTTPException(404, "rule not found")


@app.post("/api/rules/preview")
def preview_rule(payload: RuleIn) -> dict[str, Any]:
    """Plain-English preview of an unsaved draft, so the builder can show the
    user what they are about to turn on before they turn it on."""
    store = engine_of().store
    rule = payload.to_rule(store.org_id)
    try:
        validate(rule)
    except RuleValidationError as exc:
        return {"ok": False, "error": str(exc)}
    names = {u.id: u.name for u in store.users.list()}
    return {"ok": True, "description": describe(rule, names)}


@app.get("/api/notifications")
def list_notifications(
    recipient: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    include_held: bool = True,
) -> list[dict[str, Any]]:
    store = engine_of().store
    items = store.notifications.recent(
        limit=limit, recipient_id=recipient, include_buffered=include_held
    )
    return [notification_json(n) for n in items]


@app.post("/api/events")
def ingest_events(payload: dict | list[dict] = Body(...)) -> dict[str, Any]:
    """Ingest one event or a batch. Idempotent on `event_id`."""
    engine = engine_of()
    raws = payload if isinstance(payload, list) else [payload]
    results = engine.ingest_many(raws)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    fired = [n for r in results for n in r.notifications]
    return {"received": len(raws), "counts": counts, "notifications": len(fired)}


@app.post("/api/tick")
def manual_tick() -> dict[str, Any]:
    """Force an evaluation pass. The server ticks on its own; this is for
    scripted demos that want a deterministic moment."""
    fired = engine_of().tick()
    return {"notifications": len(fired), "at": iso(utcnow())}


@app.get("/api/state")
def current_state() -> dict[str, Any]:
    store = engine_of().store
    return {
        "queues": [_queue_json(q) for q in store.state.all_queues()],
        "agents": [_agent_json(a) for a in store.state.all_agents()],
        "incidents": [
            {
                "rule_id": i["rule_id"],
                "rule_name": i["rule_name"],
                "severity": i["severity"],
                "subject": f"{i['subject_type']}:{i['subject_id']}",
                "status": i["status"],
                "opened_at": i["opened_at"],
                "last_value": i["last_value"],
            }
            for i in store.engine_state.open_incidents()
        ],
        "events": store.events.count_by_status(),
    }


def _queue_json(q: QueueState) -> dict[str, Any]:
    ratio = (q.longest_wait_sec / q.sla_target_sec) if q.longest_wait_sec is not None and q.sla_target_sec else None
    return {
        "queue_id": q.queue_id,
        "updated_at": iso(q.updated_at),
        "tickets_waiting": q.tickets_waiting,
        "longest_wait_sec": q.longest_wait_sec,
        "sla_target_sec": q.sla_target_sec,
        "sla_ratio": ratio,
        "sla_display": format_value(ratio, "ratio"),
        "agents_available": q.agents_available,
        "agents_on_call": q.agents_on_call,
        "volume_last_15m": q.volume_last_15m,
        "volume_forecast_next_15m": q.volume_forecast_next_15m,
    }


def _agent_json(a: AgentState) -> dict[str, Any]:
    return {
        "agent_id": a.agent_id,
        "state": a.state,
        "state_since": iso(a.state_since),
        "queue_ids": a.queue_ids,
        "scheduled_state": a.scheduled_state,
        "in_violation": a.in_violation,
        "violation_started_at": iso(a.violation_started_at),
        "updated_at": iso(a.updated_at),
    }


def _validate(rule: Rule) -> None:
    try:
        validate(rule)
    except RuleValidationError as exc:
        raise HTTPException(400, str(exc)) from None
