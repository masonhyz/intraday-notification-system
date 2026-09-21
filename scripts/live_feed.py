"""Stream events into a running server at real wall-clock time.

    python -m uvicorn app.api:app          # in one terminal
    python scripts/live_feed.py            # in another

Unlike `app.replay`, which drives a simulated clock, this posts events stamped
`now` to the HTTP API - the same path a real producer would use. Rules are
evaluated against the wall clock, so what you watch in the UI is the system
behaving live rather than re-reading history.

Two details make a live demo possible in a couple of minutes:

* Sustain windows are real seconds. The scenario holds a bad condition for
  longer than the rule's `for_sec` so it actually fires while you watch.
* Conditions that are *already* old are expressed by backdating the condition's
  start, not the event. An adherence check stamped `now` whose
  `violation_started_at` is twenty minutes ago is a violation that is twenty
  minutes old, and fires immediately.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request


def now_iso(offset_sec: float = 0) -> str:
    moment = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_sec)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def post(api: str, event: dict) -> dict:
    request = urllib.request.Request(
        f"{api}/api/events",
        data=json.dumps(event).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def scenario(seq: int) -> list[tuple[float, dict]]:
    """(seconds from start, event). Designed to fire inside ~3 minutes."""
    def eid() -> str:
        nonlocal seq
        seq += 1
        return f"evt_live_{int(time.time())}_{seq:03d}"

    def snapshot(waiting: int, wait_sec: int, available: int) -> dict:
        return {
            "event_id": eid(), "ts": None, "type": "queue_snapshot",
            "queue_id": "billing", "tickets_waiting": waiting,
            "longest_wait_sec": wait_sec, "sla_target_sec": 120,
            "agents_available": available, "agents_on_call": 4,
            "volume_last_15m": 30, "volume_forecast_next_15m": 38,
        }

    events: list[tuple[float, dict]] = [
        # Calm: nothing should fire.
        (0, snapshot(3, 20, 3)),
        (15, snapshot(8, 60, 2)),
        # An agent who has been out of adherence for 20 minutes already.
        # Backdating the violation start is what makes this fire at once.
        (20, {"event_id": eid(), "ts": None, "type": "adherence_check",
              "agent_id": "a_19", "queue_ids": ["billing"],
              "scheduled_state": "available", "actual_state": "on_break",
              "in_violation": True, "violation_started_at": now_iso(-20 * 60)}),
    ]
    # Billing goes over its SLA and stays there. `SLA breached` needs the
    # condition to hold for 2 minutes, so keep reporting every 15 seconds.
    for i in range(14):
        at = 30 + i * 15
        events.append((at, snapshot(20 + i, 140 + i * 20, 0)))
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="time compression; 2.0 runs the scenario twice as fast")
    args = parser.parse_args(argv)

    events = scenario(0)
    total = events[-1][0] / args.speed
    print(f"Streaming {len(events)} events to {args.api} over ~{total:.0f}s.")
    print("Watch http://127.0.0.1:8000 — refresh the inbox as it runs.\n")

    started = time.monotonic()
    for offset, event in events:
        due = started + offset / args.speed
        while time.monotonic() < due:
            time.sleep(0.2)
        event["ts"] = now_iso()
        try:
            result = post(args.api, event)
        except urllib.error.URLError as exc:
            print(f"! could not reach {args.api}: {exc.reason}", file=sys.stderr)
            return 1
        fired = result.get("notifications", 0)
        mark = f"  → {fired} notification(s)" if fired else ""
        label = event.get("queue_id") or event.get("agent_id")
        print(f"[{time.monotonic() - started:5.0f}s] {event['type']:18} {label}{mark}")

    print("\nDone. Open the UI and switch 'Viewing as' to see who got what.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
