"""Time utilities for ORB trader.

All times are handled in America/New_York (US/Eastern).

We keep this small and dependency-free so it can be reused across strategy,
trading, and main loop code.
"""

from __future__ import annotations

from datetime import datetime, time

import pytz


EASTERN_TZ = pytz.timezone("America/New_York")


def now_eastern() -> datetime:
    return datetime.now(tz=EASTERN_TZ)


def parse_hhmm(hhmm: str) -> time:
    return time.fromisoformat(hhmm)


def is_past_time(now_e: datetime, hhmm: str) -> bool:
    """True if now_e (assumed eastern) is at/after HH:MM."""
    return now_e.time() >= parse_hhmm(hhmm)
