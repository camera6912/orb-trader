"""Opening Range Breakout (ORB) strategy helpers.

This module is intentionally small and testable: given intraday candles,
compute the opening range and derive the day plan (entries/stop/targets).

Times are expected in US/Eastern unless otherwise specified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from typing import Optional

import pandas as pd
import pytz
from loguru import logger

from src.utils.price_utils import es_tick_size, round_to_tick


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


class ORBState(str, Enum):
    """Simple time-based state machine for opening range capture."""

    WAITING_FOR_OPEN = "waiting"  # Before 09:30
    BUILDING_RANGE = "building"  # 09:30–09:45
    RANGE_COMPLETE = "complete"  # After 09:45


class ORBTracker:
    """Tracks /ES opening range (09:30–09:45 ET) from live quotes.

    Designed to be driven by a main loop calling `update()`.

    Edge cases handled:
      - If the process starts after 09:30, seeds range from Schwab minute history.
      - Validates quote timestamps to avoid using stale data.
      - Uses pytz America/New_York for all session cutoffs.
    """

    def __init__(
        self,
        schwab_client,
        symbol: str = "/ES",
        market_open: str = "09:30",
        range_end: str = "09:45",
        tz: str = "America/New_York",
        poll_interval_s: float = 2.0,
        max_stale_s: int = 30,
    ):
        self.schwab = schwab_client
        self.symbol = symbol
        self.market_open = market_open
        self.range_end = range_end
        self.tz = tz
        self.poll_interval_s = float(poll_interval_s)
        self.max_stale_s = int(max_stale_s)

        self.state: ORBState = ORBState.WAITING_FOR_OPEN

        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_size: Optional[float] = None
        self.range_midpoint: Optional[float] = None

        self._opening_range: Optional[OpeningRange] = None
        self._last_quote_ts: Optional[datetime] = None
        self._seeded_from_history: bool = False

    def _eastern_tz(self):
        return pytz.timezone(self.tz)

    def _session_times(self, now_e: datetime) -> tuple[datetime, datetime]:
        d = now_e.date()
        eastern = self._eastern_tz()
        start = eastern.localize(datetime.combine(d, time.fromisoformat(self.market_open)))
        end = eastern.localize(datetime.combine(d, time.fromisoformat(self.range_end)))
        return start, end

    def _compute_derived(self):
        if self.range_high is None or self.range_low is None:
            return
        self.range_size = float(self.range_high - self.range_low)
        self.range_midpoint = float((self.range_high + self.range_low) / 2.0)

    def opening_range(self) -> Optional[OpeningRange]:
        return self._opening_range

    def update(self, now: Optional[datetime] = None) -> ORBState:
        """Advance state machine and update range from quotes/history."""
        now_e = _as_eastern(now or datetime.now(), self.tz)
        start, end = self._session_times(now_e)

        # State transitions
        if now_e < start:
            self.state = ORBState.WAITING_FOR_OPEN
            return self.state

        if start <= now_e < end:
            if self.state != ORBState.BUILDING_RANGE:
                logger.info(f"ORB: entering BUILDING_RANGE ({start:%H:%M}–{end:%H:%M} {self.tz})")
            self.state = ORBState.BUILDING_RANGE

            # If we started late, seed range from history once.
            if not self._seeded_from_history:
                self._seed_from_history(now_e=now_e, window_end=min(now_e, end))
                self._seeded_from_history = True

            # Pull live quote + update high/low.
            self._update_from_quote(now_e)
            return self.state

        # now_e >= end
        if self.state != ORBState.RANGE_COMPLETE:
            self._finalize_from_history(now_e=now_e, start=start, end=end)
            self.state = ORBState.RANGE_COMPLETE
        return self.state

    def _quote_timestamp(self, raw: dict) -> Optional[datetime]:
        """Extract a quote timestamp from Schwab quote payload (ms since epoch)."""
        q = raw.get("quote", {})
        ms = q.get("quoteTimeInLong") or q.get("tradeTimeInLong")
        if not ms:
            return None
        ts_utc = datetime.fromtimestamp(int(ms) / 1000.0, tz=pytz.UTC)
        return ts_utc.astimezone(self._eastern_tz())

    def _update_from_quote(self, now_e: datetime) -> None:
        try:
            if self.symbol == "/ES":
                quotes = self.schwab.get_quotes(["/ES"])
                # Schwab resolves /ES -> /ESH26 etc.
                raw = next((v for v in quotes.values() if v.get("assetMainType") == "FUTURE"), None)
                if not raw:
                    raise RuntimeError("No FUTURE quote returned for /ES")
                price = float(raw.get("quote", {}).get("lastPrice", 0.0))
                qts = self._quote_timestamp(raw)
            else:
                raw_all = self.schwab.get_quote(self.symbol)
                raw = raw_all.get(self.symbol, {})
                price = float(raw.get("quote", {}).get("lastPrice", 0.0))
                qts = self._quote_timestamp(raw)

            if price <= 0:
                return

            if qts is not None:
                age = (now_e - qts).total_seconds()
                if age > self.max_stale_s:
                    logger.warning(f"ORB: stale quote ignored (age={age:.1f}s, ts={qts.isoformat()})")
                    return
                self._last_quote_ts = qts

            if self.range_high is None or price > self.range_high:
                self.range_high = price
            if self.range_low is None or price < self.range_low:
                self.range_low = price
            self._compute_derived()
        except Exception as e:
            logger.exception(f"ORB: quote update failed: {e}")

    def _seed_from_history(self, now_e: datetime, window_end: datetime) -> None:
        """Seed high/low from minute history for [open, window_end)."""
        try:
            candles = self.schwab.get_price_history(
                symbol=self.symbol,
                period_type="day",
                period=1,
                frequency_type="minute",
                frequency=1,
            )
            if candles.empty:
                return

            eastern = self._eastern_tz()
            if candles.index.tz is None:
                candles.index = candles.index.tz_localize(pytz.UTC)
            candles_e = candles.copy()
            candles_e.index = candles_e.index.tz_convert(eastern)

            start, _ = self._session_times(now_e)
            window = candles_e.loc[(candles_e.index >= start) & (candles_e.index < window_end)]
            if window.empty:
                return

            self.range_high = float(window["high"].max())
            self.range_low = float(window["low"].min())
            self._compute_derived()
            logger.info(
                f"ORB: seeded from history up to {window_end:%H:%M:%S} high={self.range_high:.2f} low={self.range_low:.2f}"
            )
        except Exception as e:
            logger.exception(f"ORB: seeding from history failed: {e}")

    def _finalize_from_history(self, now_e: datetime, start: datetime, end: datetime) -> None:
        """Finalize opening range at 09:45 using minute history as the source of truth."""
        candles = self.schwab.get_price_history(
            symbol=self.symbol,
            period_type="day",
            period=1,
            frequency_type="minute",
            frequency=1,
        )
        opening_range = compute_opening_range(
            candles=candles,
            trade_date=now_e,
            market_open=self.market_open,
            range_end=self.range_end,
            tz=self.tz,
        )

        self._opening_range = opening_range
        self.range_high = opening_range.high
        self.range_low = opening_range.low
        self._compute_derived()

        logger.info(
            "ORB RANGE COMPLETE: "
            f"high={opening_range.high:.2f} low={opening_range.low:.2f} "
            f"size={opening_range.size:.2f} mid={opening_range.mid:.2f} "
            f"({start:%H:%M}–{end:%H:%M} {self.tz})"
        )


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
    # Schwab history uses epoch ms and is effectively UTC; the DataFrame often
    # arrives as tz-naive. Assume UTC in that case.
    candles_e = candles.copy()
    if idx.tz is None:
        candles_e.index = candles_e.index.tz_localize(pytz.UTC)
    # Normalize to Eastern for slicing.
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

    Note: /ES trades in 0.25-point ticks. Stops must be rounded to a valid tick.
      - LONG stop: round DOWN to nearest tick (slightly wider / conservative)
      - SHORT stop: round UP to nearest tick (slightly wider / conservative)
    """

    rng = opening_range.size
    use_mid_stop = rng > target_points
    stop_level = opening_range.mid if use_mid_stop else None

    long_entry = opening_range.high
    short_entry = opening_range.low

    if use_mid_stop:
        long_stop_raw = float(stop_level)
        short_stop_raw = float(stop_level)
    else:
        long_stop_raw = float(opening_range.low)
        short_stop_raw = float(opening_range.high)

    tick = es_tick_size(symbol)
    if symbol in {"/ES", "/MES"}:
        long_stop = round_to_tick(long_stop_raw, tick_size=tick, direction="down")
        short_stop = round_to_tick(short_stop_raw, tick_size=tick, direction="up")
    else:
        long_stop = long_stop_raw
        short_stop = short_stop_raw

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
