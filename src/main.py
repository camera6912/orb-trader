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
from src.strategy.orb import ORBTracker, ORBState, build_orb_plan
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

    tracker = ORBTracker(
        schwab_client=schwab,
        symbol=symbol,
        market_open=market_open,
        range_end=range_end,
        tz="America/New_York",
        poll_interval_s=2.0,
        max_stale_s=30,
    )

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

        # Drive ORB tracker state machine (handles seeding from history + live polling)
        state = tracker.update(now)

        # Once the range is complete, build plan + place OCO exactly once
        if (not oco_placed) and state == ORBState.RANGE_COMPLETE:
            try:
                opening_range = tracker.opening_range()
                if opening_range is None:
                    raise RuntimeError("ORB range complete but opening_range is None")

                plan = build_orb_plan(symbol=symbol, opening_range=opening_range, target_points=target_points)
                broker.place_oco_entry(buy_stop=plan.long_entry, sell_stop=plan.short_entry, qty=1)
                oco_placed = True

                # Stash plan values on broker for bracket attachment after fill
                broker._orb_plan = plan  # type: ignore[attr-defined]
            except Exception as e:
                logger.exception(f"Failed to finalize ORB / place OCO: {e}; will retry")

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

        time_mod.sleep(tracker.poll_interval_s)


if __name__ == "__main__":
    main()
