"""Paper trading primitives for ORB.

This is *not* a full matching engine. It’s just enough to simulate the
ORB day-plan:
  - place an OCO entry pair (long stop / short stop)
  - on first trigger, cancel the other
  - manage attached stop/target
  - move stop to breakeven at the configured time if in profit
  - exit any open position at EOD
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from loguru import logger


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class Position:
    side: Side
    qty: int
    entry: float


@dataclass
class Bracket:
    stop: float
    target: float


@dataclass
class OCOEntry:
    buy_stop: float
    sell_stop: float
    qty: int = 1


class PaperBroker:
    def __init__(self):
        self.position: Optional[Position] = None
        self.bracket: Optional[Bracket] = None
        self.oco: Optional[OCOEntry] = None
        self.trades_today: int = 0

    def place_oco_entry(self, buy_stop: float, sell_stop: float, qty: int = 1):
        if self.trades_today >= 1:
            logger.warning("One-trade-per-day rule: refusing to place new entry")
            return
        self.oco = OCOEntry(buy_stop=buy_stop, sell_stop=sell_stop, qty=qty)
        logger.info(f"Placed OCO entry: buy_stop={buy_stop:.2f} sell_stop={sell_stop:.2f}")

    def cancel_entry(self):
        self.oco = None

    def on_price(self, price: float):
        """Call this with latest trade/last price."""
        # Entry fills
        if self.position is None and self.oco is not None:
            if price >= self.oco.buy_stop:
                self._open(side=Side.LONG, qty=self.oco.qty, entry=self.oco.buy_stop)
                self.oco = None
            elif price <= self.oco.sell_stop:
                self._open(side=Side.SHORT, qty=self.oco.qty, entry=self.oco.sell_stop)
                self.oco = None

        # Manage exits
        if self.position and self.bracket:
            if self.position.side == Side.LONG:
                if price <= self.bracket.stop:
                    self._close(price, reason="STOP")
                elif price >= self.bracket.target:
                    self._close(price, reason="TARGET")
            else:
                if price >= self.bracket.stop:
                    self._close(price, reason="STOP")
                elif price <= self.bracket.target:
                    self._close(price, reason="TARGET")

    def attach_bracket(self, stop: float, target: float):
        if not self.position:
            raise RuntimeError("No position to attach bracket to")
        self.bracket = Bracket(stop=stop, target=target)
        logger.info(f"Attached bracket: stop={stop:.2f} target={target:.2f}")

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

    def exit_market(self, last_price: float, reason: str = "EOD"):
        if self.position:
            self._close(last_price, reason=reason)
        self.oco = None

    def _open(self, side: Side, qty: int, entry: float):
        self.position = Position(side=side, qty=qty, entry=entry)
        self.trades_today += 1
        logger.info(f"FILLED {side} qty={qty} entry={entry:.2f}")

    def _close(self, price: float, reason: str):
        assert self.position is not None
        pnl = (price - self.position.entry) * (1 if self.position.side == Side.LONG else -1)
        logger.info(
            f"CLOSE {self.position.side} qty={self.position.qty} exit={price:.2f} PnL={pnl:.2f} reason={reason}"
        )
        self.position = None
        self.bracket = None
