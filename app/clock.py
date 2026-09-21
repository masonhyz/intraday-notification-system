"""Time source.

Everything in the engine takes `now` explicitly or reads it from a Clock, so
that replaying a day of history is byte-for-byte identical to running live.
Tests and the replay CLI use ManualClock; the API server uses SystemClock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return utcnow()


class ManualClock:
    """A clock the caller advances. Never goes backwards."""

    def __init__(self, start: datetime) -> None:
        self._now = _as_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        ts = _as_utc(ts)
        if ts > self._now:
            self._now = ts

    def advance(self, seconds: float) -> datetime:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)
        return self._now


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def iso(ts: datetime | None) -> str | None:
    """UTC ISO-8601 with a trailing Z. Lexicographic order == chronological order."""
    if ts is None:
        return None
    return _as_utc(ts).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_ts(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _as_utc(datetime.fromisoformat(text))
