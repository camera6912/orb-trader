"""ORB Trader (paper) entrypoint.

This is a minimal runnable skeleton that:
  - authenticates with Schwab
  - computes the 09:30–09:45 opening range
  - at 09:45 places an OCO entry (paper)
  - manages bracket exits + breakeven rule + EOD exit

It’s intentionally conservative and debuggable; you can evolve it into live
order routing later.
"""

from __future__ import annotations

import time as time_mod
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import pytz
import yaml
from loguru import logger

from src.data.schwab import SchwabClient
from src.strategy.orb import compute_opening_range, build_orb_plan
from src.trading.paper import PaperBroker


@dataclass
class Settings:
    symbol: str
    market_open: str
    range_end: str
    be_check_time: str
    eod_exit: str
    target_points: float
    gap_threshold_pct: float


def load_settings(path: str = "config/settings.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _t(hhmm: str) -> time:
    return time.fromisoformat(hhmm)


def _now_eastern() -> datetime:
    return datetime.now(tz=pytz.timezone("US/Eastern"))


def main():
    settings = load_settings()

    symbol = settings["symbol"]
    market_open = settings["market_open"]
    range_end = settings["range_end"]
    be_check_time = settings["be_check_time"]
    eod_exit = settings["eod_exit"]
    target_points = float(settings["target_points"])

    logger.add("logs/orb-trader.log", rotation="1 day", retention="14 days")

    schwab = SchwabClient(config_path="config/secrets.yaml")
    if not schwab.authenticate(interactive=True):
        raise SystemExit(1)

    broker = PaperBroker()

    oco_placed = False
    bracket_attached = False
    be_done = False

    while True:
        now = _now_eastern()
        t = now.time()

        # Pull last price
        try:
            q = schwab.get_es_quote() if symbol == "/ES" else schwab.get_quote(symbol)
            last_price = float(q.get("price") if symbol == "/ES" else q.get(symbol, {}).get("quote", {}).get("lastPrice", 0.0))
        except Exception as e:
            logger.exception(f"Quote error: {e}")
            time_mod.sleep(5)
            continue

        # After market open but before range_end: keep updating range calculation cache
        if t >= _t(market_open) and t < _t(range_end):
            # We compute on-demand at range_end.
            pass

        # At/after range_end, compute OR and place OCO once
        if (not oco_placed) and t >= _t(range_end):
            try:
                candles = schwab.get_price_history(
                    symbol=symbol,
                    period_type="day",
                    period=1,
                    frequency_type="minute",
                    frequency=1,
                )
                opening_range = compute_opening_range(
                    candles=candles,
                    trade_date=now,
                    market_open=market_open,
                    range_end=range_end,
                )
                plan = build_orb_plan(symbol=symbol, opening_range=opening_range, target_points=target_points)

                logger.info(
                    f"Opening range: high={opening_range.high:.2f} low={opening_range.low:.2f} size={opening_range.size:.2f}"
                )
                broker.place_oco_entry(buy_stop=plan.long_entry, sell_stop=plan.short_entry, qty=1)
                oco_placed = True
                # Stash plan values on broker for bracket attachment after fill
                broker._orb_plan = plan  # type: ignore[attr-defined]
            except Exception as e:
                logger.exception(f"Failed to compute opening range / place OCO: {e}
Will retry.")

        # Drive paper broker with price updates
        broker.on_price(last_price)

        # Attach bracket immediately after fill
        if broker.position and (not bracket_attached) and hasattr(broker, "_orb_plan"):
            plan = broker._orb_plan  # type: ignore[attr-defined]
            if broker.position.side.value == "LONG":
                broker.attach_bracket(stop=plan.long_stop, target=plan.long_target)
            else:
                broker.attach_bracket(stop=plan.short_stop, target=plan.short_target)
            bracket_attached = True

        # Breakeven check at configured time
        if (not be_done) and t >= _t(be_check_time):
            broker.move_stop_to_breakeven_if_in_profit(last_price)
            be_done = True

        # EOD exit
        if t >= _t(eod_exit):
            broker.exit_market(last_price, reason="EOD")
            logger.info("EOD exit completed; shutting down")
            break

        time_mod.sleep(2)


if __name__ == "__main__":
    main()
