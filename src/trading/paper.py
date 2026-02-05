"""Paper trading primitives for ORB.

This is *not* a full matching engine. It’s just enough to simulate the
ORB day-plan:
  - place an OCO entry pair (long stop / short stop)
  - on first trigger, cancel the other
  - manage attached stop/target
  - move stop to breakeven at the configured time if in profit
  - exit any open position at EOD

Sprint 3 adds:
  - ORB-specific OCO entry placement (range boundaries + buffer)
  - stop/target calculation rules
  - one-trade-per-day guard
  - simple paper fill logic based on *crossing* the stop price (up/down)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

import pytz
from loguru import logger

from src.utils.price_utils import round_to_tick

from src.notifications.alerts import ExitSummary, format_entry, format_exit


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class Position:
    side: Side
    qty: int
    entry: float
    entry_time: datetime


@dataclass
class Bracket:
    stop: float
    target: float


@dataclass
class OCOEntry:
    """Pending ORB entry orders.

    buy_stop / sell_stop are the actual trigger prices (range boundary + buffer).
    range_* are stored so we can compute bracket orders on fill.
    """

    buy_stop: float
    sell_stop: float

    range_high: float
    range_low: float
    range_mid: float
    range_size: float

    target_points: float = 20.0
    qty: int = 1
    placed_time: Optional[datetime] = None


class PaperBroker:
    def __init__(
        self,
        *,
        notifier=None,
        symbol: str = "/ES",
        point_value: float = 50.0,
    ):
        # notifier: object with .send_message(str) -> bool
        self.notifier = notifier
        self.symbol = symbol
        self.point_value = float(point_value)

        self.position: Optional[Position] = None
        self.bracket: Optional[Bracket] = None
        self.oco: Optional[OCOEntry] = None

        self.trade_taken_today: bool = False

        # Tracks whether we've moved the active stop to breakeven for this trade.
        self._breakeven_stop_active: bool = False

        self._last_price: Optional[float] = None
        self._last_unrealized_pnl: Optional[float] = None

    def place_orb_oco(
        self,
        *,
        range_high: float,
        range_low: float,
        buffer: float = 0.25,
        target_points: float = 20.0,
        qty: int = 1,
        now: Optional[datetime] = None,
    ):
        """Place the ORB OCO entry orders once the opening range is complete."""

        if self.trade_taken_today:
            logger.warning("One-trade-per-day rule: refusing to place new entry")
            return

        # Entry stop orders must be on a valid tick.
        # LONG entry triggers above range_high: round UP (don’t accidentally place inside the range).
        # SHORT entry triggers below range_low: round DOWN.
        buy_stop = round_to_tick(float(range_high + buffer), tick_size=0.25, direction="up")
        sell_stop = round_to_tick(float(range_low - buffer), tick_size=0.25, direction="down")
        range_size = float(range_high - range_low)
        range_mid = float((range_high + range_low) / 2.0)
        ts = now or datetime.now(tz=pytz.timezone("US/Eastern"))

        self.oco = OCOEntry(
            buy_stop=buy_stop,
            sell_stop=sell_stop,
            range_high=float(range_high),
            range_low=float(range_low),
            range_mid=range_mid,
            range_size=range_size,
            target_points=float(target_points),
            qty=int(qty),
            placed_time=ts,
        )
        logger.info(
            "Placed ORB OCO entry: "
            f"buy_stop={buy_stop:.2f} sell_stop={sell_stop:.2f} "
            f"(range_high={range_high:.2f} range_low={range_low:.2f} buffer={buffer:.2f})"
        )

    def cancel_entry(self, reason: str = "CANCEL"):
        if self.oco is not None:
            logger.info(f"Canceled pending OCO entry (reason={reason})")
        self.oco = None

    def reset_for_new_day(self, *, reason: str = "DAILY_RESET"):
        """Clear all intraday state so we're ready for the next session."""
        if self.position or self.bracket:
            logger.warning(f"Resetting broker state with an open position (reason={reason})")
        if self.oco is not None:
            logger.info(f"Clearing pending OCO entry (reason={reason})")
        self.position = None
        self.bracket = None
        self.oco = None
        self.trade_taken_today = False
        self._breakeven_stop_active = False
        self._last_price = None
        self._last_unrealized_pnl = None

    def on_price(self, price: float, now: Optional[datetime] = None):
        """Call this with latest trade/last price."""
        ts = now or datetime.now(tz=pytz.timezone("US/Eastern"))

        # Entry fills (crossing logic)
        if self.position is None and self.oco is not None and self._last_price is not None:
            if self._last_price < self.oco.buy_stop <= price:
                logger.info(
                    f"OCO trigger: LONG entry hit @ {self.oco.buy_stop:.2f}; "
                    f"canceling SHORT stop @ {self.oco.sell_stop:.2f}"
                )
                self._open(side=Side.LONG, qty=self.oco.qty, entry=self.oco.buy_stop, now=ts)
                # OCO behavior: cancel the other leg
                self.oco = None
            elif self._last_price > self.oco.sell_stop >= price:
                logger.info(
                    f"OCO trigger: SHORT entry hit @ {self.oco.sell_stop:.2f}; "
                    f"canceling LONG stop @ {self.oco.buy_stop:.2f}"
                )
                self._open(side=Side.SHORT, qty=self.oco.qty, entry=self.oco.sell_stop, now=ts)
                self.oco = None

        # If this is the first price tick after OCO placement, we can’t do crossing;
        # fall back to simple trigger.
        if self.position is None and self.oco is not None and self._last_price is None:
            if price >= self.oco.buy_stop:
                logger.info(
                    f"OCO trigger (no-cross fallback): LONG entry hit @ {self.oco.buy_stop:.2f}; "
                    f"canceling SHORT stop @ {self.oco.sell_stop:.2f}"
                )
                self._open(side=Side.LONG, qty=self.oco.qty, entry=self.oco.buy_stop, now=ts)
                self.oco = None
            elif price <= self.oco.sell_stop:
                logger.info(
                    f"OCO trigger (no-cross fallback): SHORT entry hit @ {self.oco.sell_stop:.2f}; "
                    f"canceling LONG stop @ {self.oco.buy_stop:.2f}"
                )
                self._open(side=Side.SHORT, qty=self.oco.qty, entry=self.oco.sell_stop, now=ts)
                self.oco = None

        # Manage exits
        if self.position and self.bracket:
            if self.position.side == Side.LONG:
                if price <= self.bracket.stop:
                    self._close(price, reason="stop", now=ts)
                elif price >= self.bracket.target:
                    self._close(price, reason="target", now=ts)
            else:
                if price >= self.bracket.stop:
                    self._close(price, reason="stop", now=ts)
                elif price <= self.bracket.target:
                    self._close(price, reason="target", now=ts)

            # P&L logging (throttled)
            self._log_unrealized_pnl(price)

        self._last_price = float(price)

    def move_stop_to_breakeven_if_in_profit(self, last_price: float, *, now: Optional[datetime] = None):
        ts = now or datetime.now(tz=pytz.timezone("US/Eastern"))
        if not self.position or not self.bracket:
            return
        if self._breakeven_stop_active:
            return

        if self.position.side == Side.LONG and last_price > self.position.entry:
            if self.bracket.stop < self.position.entry:
                # LONG stop rounds DOWN (conservative/wider)
                self.bracket.stop = round_to_tick(self.position.entry, tick_size=0.25, direction="down")
                self._breakeven_stop_active = True
                logger.info(
                    f"Breakeven check: moved stop to breakeven @ {self.bracket.stop:.2f} "
                    f"(entry={self.position.entry:.2f} last={last_price:.2f} time={ts.isoformat()})"
                )
        elif self.position.side == Side.SHORT and last_price < self.position.entry:
            if self.bracket.stop > self.position.entry:
                # SHORT stop rounds UP (conservative/wider)
                self.bracket.stop = round_to_tick(self.position.entry, tick_size=0.25, direction="up")
                self._breakeven_stop_active = True
                logger.info(
                    f"Breakeven check: moved stop to breakeven @ {self.bracket.stop:.2f} "
                    f"(entry={self.position.entry:.2f} last={last_price:.2f} time={ts.isoformat()})"
                )

    def exit_market(self, last_price: float, reason: str = "eod", now: Optional[datetime] = None):
        """Exit any open position at market and cancel any unfilled entry orders."""
        ts = now or datetime.now(tz=pytz.timezone("US/Eastern"))
        if self.position:
            self._close(last_price, reason=reason, now=ts)
        if self.oco is not None:
            self.cancel_entry(reason=reason)

    def _compute_stop_target(self, side: Side, entry: float, oco: OCOEntry) -> tuple[float, float]:
        # Target: fixed points from entry
        target = float(entry + oco.target_points) if side == Side.LONG else float(entry - oco.target_points)

        # Stop: opposite end of range, unless range > target_points → midpoint
        if oco.range_size > oco.target_points:
            stop_raw = float(oco.range_mid)
        else:
            stop_raw = float(oco.range_low) if side == Side.LONG else float(oco.range_high)

        # /ES tick rounding (conservative/wider):
        #   LONG stop rounds DOWN; SHORT stop rounds UP.
        if side == Side.LONG:
            stop = round_to_tick(stop_raw, tick_size=0.25, direction="down")
        else:
            stop = round_to_tick(stop_raw, tick_size=0.25, direction="up")

        return float(stop), target

    def _open(self, side: Side, qty: int, entry: float, now: datetime):
        self.trade_taken_today = True
        self.position = Position(side=side, qty=qty, entry=float(entry), entry_time=now)

        if self.oco is None:
            # Open without an OCO context (should be rare); don’t attach bracket.
            self.bracket = None
            logger.info(f"FILLED {side} qty={qty} entry={entry:.2f}")
            return

        stop, target = self._compute_stop_target(side=side, entry=float(entry), oco=self.oco)
        self.bracket = Bracket(stop=stop, target=target)

        logger.info(
            f"FILLED {side} qty={qty} entry={entry:.2f} "
            f"stop={stop:.2f} target={target:.2f} "
            f"(entry_time={now.isoformat()})"
        )

        # Community alert (best-effort)
        if self.notifier is not None:
            try:
                msg = format_entry(side=side.value, entry=float(entry), stop=float(stop), target=float(target))
                self.notifier.send_message(msg)
            except Exception as e:
                logger.error(f"Campfire entry alert failed: {e}")

        # Reset unrealized PnL throttle
        self._last_unrealized_pnl = None
        self._breakeven_stop_active = False

    def _close(self, price: float, reason: str, now: datetime):
        assert self.position is not None
        pnl = (price - self.position.entry) * (1 if self.position.side == Side.LONG else -1)
        # Normalize exit reasons for downstream reporting.
        exit_reason = reason
        if reason == "stop" and self._breakeven_stop_active and self.bracket is not None:
            # If we moved the stop up/down to entry, a subsequent stop-out is a breakeven exit.
            if abs(self.bracket.stop - self.position.entry) < 1e-9:
                exit_reason = "breakeven_stop"

        duration_s = (now - self.position.entry_time).total_seconds()

        logger.info(
            f"CLOSE {self.position.side} qty={self.position.qty} exit={price:.2f} "
            f"PnL={pnl:.2f} exit_reason={exit_reason} "
            f"duration_s={duration_s:.0f} "
            f"(entry_time={self.position.entry_time.isoformat()} exit_time={now.isoformat()})"
        )

        # Community alert (best-effort)
        if self.notifier is not None:
            try:
                dollars = pnl * self.point_value * float(self.position.qty)
                summary = ExitSummary(
                    exit_reason=exit_reason,
                    pnl_points=float(pnl),
                    pnl_dollars=float(dollars),
                    duration_s=float(duration_s),
                )
                msg = format_exit(
                    side=self.position.side.value,
                    entry=float(self.position.entry),
                    exit_price=float(price),
                    summary=summary,
                )
                self.notifier.send_message(msg)
            except Exception as e:
                logger.error(f"Campfire exit alert failed: {e}")

        self.position = None
        self.bracket = None
        self._breakeven_stop_active = False
        self._last_unrealized_pnl = None

    def _log_unrealized_pnl(self, last_price: float, min_change: float = 1.0):
        if not self.position:
            return
        pnl = (last_price - self.position.entry) * (1 if self.position.side == Side.LONG else -1)
        if self._last_unrealized_pnl is None or abs(pnl - self._last_unrealized_pnl) >= min_change:
            logger.debug(f"Unrealized PnL: {pnl:.2f} (last={last_price:.2f} entry={self.position.entry:.2f})")
            self._last_unrealized_pnl = float(pnl)
