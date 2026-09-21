# Intraday notification system

Contact centre operations move fast. This service watches the live event feed
from the floor — queue snapshots, agent state changes, adherence checks — and
tells the specific person who can do something about it, without becoming the
channel everyone mutes.

The design reasoning, trade-offs and what I left out are in
**[docs/DESIGN.md](docs/DESIGN.md)**.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                  # fastapi, uvicorn, pydantic, pytest

# 1. replay the sample morning and watch the notifications fire
python -m app.replay data/events.jsonl --fresh

# 2. browse the result: inbox, rule builder, live state
python -m uvicorn app.api:app --reload   # → http://127.0.0.1:8000

# 3. the tests
python -m pytest
```

`--fresh` starts from an empty database and seeds a demo team (two team leads,
a head of support, eight agents) with a starter rule book.

## What you should see

96 raw events across 90 minutes become **44 notifications**, and the interesting
part is the arithmetic that got them there:

| | |
|---|---|
| `billing` sits over its SLA target from 09:30 to 10:15 | Its lead gets **4 messages**: one alert, two reminders 15 minutes apart, one all-clear. Not one per 30-second snapshot. |
| An agent drifts out of adherence for 35 minutes | **The agent** is nudged at the 10-minute mark and told when they are back. Their **lead** is only pulled in at 30 minutes, when it is clear they have not fixed it themselves. |
| Four agents are stuck on calls over 45 minutes | All four fire **while the call is still running**. The feed only reports call duration after a call ends, so this comes from a periodic tick, not an event. |
| The head of support is on most of the same rules | She is interrupted **only by criticals**. Everything else arrives as a quarter-hourly digest, with things that flared and recovered on their own listed separately. |
| The feed contains a redelivered event, an out-of-order snapshot, a null `queue_ids` and a violation with no start time | All four are handled, and all four are covered by tests. |

Switch the **Viewing as** dropdown in the UI to see the same morning from
Dana's, Marco's, Priya's and Nina's point of view — the routing is the product.

## Where things live

```
app/
  models.py      event + rule types; lenient event parsing
  state.py       folding events into per-subject state (the messy-feed logic)
  metrics.py     the metric catalog rules are written against
  rules.py       validation, and rules rendered as English
  templates.py   the presets the rule builder starts from
  engine.py      ingest → evaluate → the incident state machine
  routing.py     audience → people
  notify.py      rendering, channel stubs, digests
  store.py       repositories over SQLite
  schema.sql     the data model, with the reasoning in comments
  api.py         HTTP API
  static/        the web UI (no build step)
  replay.py      feed a .jsonl through the engine on a simulated clock
tests/           106 tests; see docs/DESIGN.md for what they cover and why
```

## Notes

* **Delivery is stubbed**, as the brief allows. Notifications are persisted,
  printed to the console by the replay CLI, and served at `/api/notifications`
  and in the UI. `app/notify.py` has console, file and in-memory channels
  behind one `Channel` interface; a real Slack transport is one more class.
* **Auth, authorisation and multi-tenancy plumbing are out of scope**, but
  `org_id` is the leading column on every table so it never has to be
  retrofitted.
* **SQLite** keeps the whole thing runnable with no infrastructure. The schema
  is written for Postgres; see DESIGN.md for what changes at scale.
