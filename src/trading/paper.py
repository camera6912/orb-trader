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
    def __init__(self):
        self.position: Optional[Position] = None
        self.bracket: Optional[Bracket] = None
        self.oco: Optional[OCOEntry] = None

        self.trade_taken_today: bool = False

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

        buy_stop = float(range_high + buffer)
        sell_stop = float(range_low - buffer)
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
                    self._close(price, reason="STOP", now=ts)
                elif price >= self.bracket.target:
                    self._close(price, reason="TARGET", now=ts)
            else:
                if price >= self.bracket.stop:
                    self._close(price, reason="STOP", now=ts)
                elif price <= self.bracket.target:
                    self._close(price, reason="TARGET", now=ts)

            # P&L logging (throttled)
            self._log_unrealized_pnl(price)

        self._last_price = float(price)

    def move_stop_to_breakeven_if_in_profit(self, last_price: float):
        if not self.position or not self.bracket:
            return
        if self.position.side == Side.LONG and last_price > self.position.entry:
            if self.bracket.stop < self.position.entry:
                self.bracket.stop = self.position.entry
                logger.info(f"Moved stop to breakeven: {self.bracket.stop:.2f}")
        elif self.position.side == Side.SHORT and last_price < self.position.entry:
            if self.bracket.stop > self.position.entry:
                self.bracket.stop = self.position.entry
                logger.info(f"Moved stop to breakeven: {self.bracket.stop:.2f}")

    def exit_market(self, last_price: float, reason: str = "EOD", now: Optional[datetime] = None):
        ts = now or datetime.now(tz=pytz.timezone("US/Eastern"))
        if self.position:
            self._close(last_price, reason=reason, now=ts)
        self.oco = None

    def _compute_stop_target(self, side: Side, entry: float, oco: OCOEntry) -> tuple[float, float]:
        # Target: fixed points from entry
        target = float(entry + oco.target_points) if side == Side.LONG else float(entry - oco.target_points)

        # Stop: opposite end of range, unless range > target_points → midpoint
        if oco.range_size > oco.target_points:
            stop = float(oco.range_mid)
        else:
            stop = float(oco.range_low) if side == Side.LONG else float(oco.range_high)

        return stop, target

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

        # Reset unrealized PnL throttle
        self._last_unrealized_pnl = None

    def _close(self, price: float, reason: str, now: datetime):
        assert self.position is not None
        pnl = (price - self.position.entry) * (1 if self.position.side == Side.LONG else -1)
        logger.info(
            f"CLOSE {self.position.side} qty={self.position.qty} exit={price:.2f} "
            f"PnL={pnl:.2f} reason={reason} (exit_time={now.isoformat()})"
        )
        self.position = None
        self.bracket = None
        self._last_unrealized_pnl = None

    def _log_unrealized_pnl(self, last_price: float, min_change: float = 1.0):
        if not self.position:
            return
        pnl = (last_price - self.position.entry) * (1 if self.position.side == Side.LONG else -1)
        if self._last_unrealized_pnl is None or abs(pnl - self._last_unrealized_pnl) >= min_change:
            logger.debug(f"Unrealized PnL: {pnl:.2f} (last={last_price:.2f} entry={self.position.entry:.2f})")
            self._last_unrealized_pnl = float(pnl)
