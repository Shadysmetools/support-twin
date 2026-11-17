"""Time source. All code asks a Clock for 'now' so tests can move time by hand.

Egypt has summer time, so we always use the Africa/Cairo zone, never a fixed +02:00.

Compare and subtract in UTC. Python ignores the UTC offset when both datetimes carry the same tzinfo object, so
two Cairo datetimes compare, subtract and add a timedelta by WALL CLOCK: a span across the summer-time change is an
hour off, and inside the repeated hour (the last Thursday of October, 23:00 to 24:00) 23:56+03:00 sorts after
23:01+02:00. `utc()`, `elapsed()` and `shift()` below are the only safe way to do that maths. Cairo is for the
calendar only (working hours, "next working day 10:15") and for display.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")
UTC = timezone.utc


def utc(dt: datetime) -> datetime:
    """The same instant in UTC (aware). Use it on both sides of every comparison."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime: use clock.now()")
    return dt.astimezone(UTC)


def elapsed(start: datetime, end: datetime) -> timedelta:
    """Real time from start to end (UTC maths, so the summer-time change never adds or loses an hour)."""
    return utc(end) - utc(start)


def shift(dt: datetime, delta: timedelta) -> datetime:
    """dt plus a real-time delta, returned in Cairo."""
    return (utc(dt) + delta).astimezone(CAIRO)


def latest(*dts: datetime) -> datetime:
    """The latest instant (not the latest wall-clock time)."""
    return max(dts, key=utc)


def earliest(*dts: datetime) -> datetime:
    return min(dts, key=utc)


class Clock(Protocol):
    def now(self) -> datetime:
        """Aware datetime in Africa/Cairo."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(CAIRO)


class FakeClock:
    """A clock you move by hand in tests."""

    def __init__(self, start: datetime | None = None):
        start = start or datetime(2026, 11, 1, 14, 40, tzinfo=CAIRO)  # a Sunday, in working hours
        if start.tzinfo is None:
            start = start.replace(tzinfo=CAIRO)
        self._now = start.astimezone(CAIRO)

    def now(self) -> datetime:
        return self._now

    def advance(self, *, minutes: float = 0, seconds: float = 0, hours: float = 0, days: float = 0) -> datetime:
        self._now = shift(self._now, timedelta(minutes=minutes, seconds=seconds, hours=hours, days=days))
        return self._now

    def set(self, dt: datetime) -> datetime:
        self._now = (dt if dt.tzinfo else dt.replace(tzinfo=CAIRO)).astimezone(CAIRO)
        return self._now
