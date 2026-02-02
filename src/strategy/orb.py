"""Opening Range Breakout (ORB) strategy helpers.

This module is intentionally small and testable: given intraday candles,
compute the opening range and derive the day plan (entries/stop/targets).

Times are expected in US/Eastern unless otherwise specified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional, Tuple

import pandas as pd
import pytz


@dataclass(frozen=True)
class OpeningRange:
    start: datetime
    end: datetime
    high: float
    low: float

    @property
    def size(self) -> float:
        return float(self.high - self.low)

    @property
    def mid(self) -> float:
        return float((self.high + self.low) / 2.0)


@dataclass(frozen=True)
class ORBPlan:
    symbol: str
    opening_range: OpeningRange

    long_entry: float
    long_stop: float
    long_target: float

    short_entry: float
    short_stop: float
    short_target: float


def _as_eastern(dt: datetime, tz: str = "US/Eastern") -> datetime:
    eastern = pytz.timezone(tz)
    if dt.tzinfo is None:
        return eastern.localize(dt)
    return dt.astimezone(eastern)


def compute_opening_range(
    candles: pd.DataFrame,
    trade_date: datetime,
    market_open: str = "09:30",
    range_end: str = "09:45",
    tz: str = "US/Eastern",
) -> OpeningRange:
    """Compute opening range high/low from 1-min candles.

    Args:
        candles: DataFrame indexed by timezone-aware datetimes with columns
            [open, high, low, close, volume].
        trade_date: A datetime on the session date (date component is used).
        market_open: HH:MM Eastern.
        range_end: HH:MM Eastern.
        tz: timezone name.

    Returns:
        OpeningRange

    Raises:
        ValueError if candles don’t cover the range.
    """
    d = _as_eastern(trade_date, tz).date()
    start_t = time.fromisoformat(market_open)
    end_t = time.fromisoformat(range_end)

    eastern = pytz.timezone(tz)
    start = eastern.localize(datetime.combine(d, start_t))
    end = eastern.localize(datetime.combine(d, end_t))

    if candles.empty:
        raise ValueError("No candles provided")

    idx = candles.index
    if idx.tz is None:
        raise ValueError("Candles index must be timezone-aware")

    # Schwab history often returns UTC; normalize to Eastern for slicing.
    candles_e = candles.copy()
    candles_e.index = candles_e.index.tz_convert(eastern)

    window = candles_e.loc[(candles_e.index >= start) & (candles_e.index < end)]
    if window.empty:
        raise ValueError(f"No candles in opening range window {start}–{end}")

    high = float(window["high"].max())
    low = float(window["low"].min())

    return OpeningRange(start=start, end=end, high=high, low=low)


def build_orb_plan(
    symbol: str,
    opening_range: OpeningRange,
    target_points: float = 20.0,
) -> ORBPlan:
    """Create the ORB order plan.

    Rules (per spec):
      - Entries at range boundaries.
      - Target is fixed target_points.
      - Stop is opposite side of range, OR midpoint if the range > target_points.
    """

    rng = opening_range.size
    use_mid_stop = rng > target_points
    stop_level = opening_range.mid if use_mid_stop else None

    long_entry = opening_range.high
    short_entry = opening_range.low

    if use_mid_stop:
        long_stop = float(stop_level)
        short_stop = float(stop_level)
    else:
        long_stop = opening_range.low
        short_stop = opening_range.high

    long_target = float(long_entry + target_points)
    short_target = float(short_entry - target_points)

    return ORBPlan(
        symbol=symbol,
        opening_range=opening_range,
        long_entry=long_entry,
        long_stop=long_stop,
        long_target=long_target,
        short_entry=short_entry,
        short_stop=short_stop,
        short_target=short_target,
    )


def gap_pct(prev_close: float, today_open: float) -> float:
    if prev_close == 0:
        return 0.0
    return abs((today_open - prev_close) / prev_close) * 100.0


def is_gap_day(prev_close: Optional[float], today_open: Optional[float], threshold_pct: float) -> bool:
    if prev_close is None or today_open is None:
        return False
    return gap_pct(prev_close, today_open) >= threshold_pct
