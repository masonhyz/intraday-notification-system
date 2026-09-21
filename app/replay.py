"""Replay a JSONL event feed through the engine.

    python -m app.replay data/events.jsonl

Replay is the same code path as live ingest; the only difference is the clock.
A ManualClock is pulled forward by each event's timestamp, and a tick is
injected every `--tick` simulated seconds in between, so a rule that says
"on a call for 45 minutes" fires at the minute it becomes true rather than
whenever the next event happens to arrive.

That equivalence is the point: the end-to-end test replays this file and
asserts on the exact notifications, which is only meaningful because replay and
production ingest share everything but the time source.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from .clock import ManualClock, parse_ts
from .db import DEFAULT_DB_PATH, connect, reset
from .engine import Engine
from .notify import ConsoleChannel, FileChannel
from .seed import seed
from .store import Store

DEFAULT_TICK_SEC = 30


def read_events(path: Path) -> Iterator[dict]:
    with path.open() as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"! line {line_no}: not valid JSON ({exc.msg})", file=sys.stderr)


def replay(
    engine: Engine,
    events: list[dict],
    *,
    tick_sec: int = DEFAULT_TICK_SEC,
    trailing_sec: int = 0,
) -> dict[str, int]:
    """Feed events through the engine in file order, ticking as time passes."""
    clock = engine.clock
    counts = {"applied": 0, "stale": 0, "duplicate": 0, "rejected": 0, "notifications": 0}

    for raw in events:
        ts = parse_ts(raw.get("ts")) if isinstance(raw.get("ts"), str) else None
        if ts is not None and isinstance(clock, ManualClock):
            # Catch up to the event one tick at a time so time-based rules fire
            # at the right moment, not late.
            while clock.now() + timedelta(seconds=tick_sec) <= ts:
                clock.advance(tick_sec)
                counts["notifications"] += len(engine.tick())
        result = engine.ingest(raw)
        counts[result.status] = counts.get(result.status, 0) + 1
        counts["notifications"] += len(result.notifications)

    if trailing_sec and isinstance(clock, ManualClock):
        end = clock.now() + timedelta(seconds=trailing_sec)
        while clock.now() < end:
            clock.advance(tick_sec)
            counts["notifications"] += len(engine.tick())

    # Anything still held for a digest should be sent before we stop the world.
    counts["notifications"] += len(engine.notifier.flush_digests(clock.now(), force=True))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("events", type=Path, help="path to a .jsonl event feed")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--fresh", action="store_true", help="start from an empty database")
    parser.add_argument("--tick", type=int, default=DEFAULT_TICK_SEC,
                        help="simulated seconds between evaluation ticks")
    parser.add_argument("--trailing", type=int, default=600,
                        help="simulated seconds to keep ticking after the last event")
    parser.add_argument("--quiet", action="store_true", help="do not print notifications")
    parser.add_argument("--log", type=Path, default=None,
                        help="also append notifications to this JSONL file")
    args = parser.parse_args(argv)

    if args.fresh:
        reset(args.db)

    events = list(read_events(args.events))
    if not events:
        print("no events to replay", file=sys.stderr)
        return 1

    first_ts = next((parse_ts(e["ts"]) for e in events if isinstance(e.get("ts"), str)), None)
    store = Store(connect(args.db))
    seed(store)

    channels = []
    if not args.quiet:
        channels.append(ConsoleChannel())
    if args.log:
        channels.append(FileChannel(args.log))

    engine = Engine(store, clock=ManualClock(first_ts), channels=channels)

    if not args.quiet:
        print(f"Replaying {len(events)} events from {args.events} "
              f"(tick every {args.tick}s of simulated time)\n")
    counts = replay(engine, events, tick_sec=args.tick, trailing_sec=args.trailing)

    print(
        f"\n{counts['applied']} applied · {counts.get('stale', 0)} stale · "
        f"{counts.get('duplicate', 0)} duplicate · {counts.get('rejected', 0)} rejected"
        f"  →  {store.notifications.count()} notifications"
    )
    print(f"Database: {args.db}   (run `uvicorn app.api:app` to browse them)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
