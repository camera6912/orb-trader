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
from datetime import datetime, time, date as date_type
from pathlib import Path

import yaml
from loguru import logger

from src.data.schwab import SchwabClient
from src.notifications.alerts import format_range_set, format_skip_day
from src.notifications.campfire import notifier_from_config
from src.strategy.orb import ORBTracker, ORBState
from src.strategy.skip_days import should_skip_today
from src.trading.paper import PaperBroker
from src.utils.time_utils import is_past_time, now_eastern, parse_hhmm
import pytz
from datetime import timedelta


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


def get_prev_close_candle(schwab: SchwabClient, symbol: str, today: datetime.date):
    """Get the previous trading day's last 15-min RTH candle.
    
    Returns (high, low) or (None, None) if unavailable.
    """
    try:
        et = pytz.timezone('America/New_York')
        
        # Get 5 days of 15-min data
        response = schwab.client.get_price_history(
            symbol,
            period_type=schwab.client.PriceHistory.PeriodType.DAY,
            period=schwab.client.PriceHistory.Period.FIVE_DAYS,
            frequency_type=schwab.client.PriceHistory.FrequencyType.MINUTE,
            frequency=schwab.client.PriceHistory.Frequency.EVERY_FIFTEEN_MINUTES
        )
        
        if response.status_code != 200:
            logger.warning(f"Failed to get prev close candle: {response.text}")
            return None, None
        
        import pandas as pd
        data = response.json()
        candles = data.get('candles', [])
        if not candles:
            return None, None
        
        df = pd.DataFrame(candles)
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df = df.set_index('datetime')
        df = df.tz_localize('UTC').tz_convert(et)
        
        # Filter to RTH only (9:30-16:00)
        def is_rth(dt):
            t = dt.time()
            return t >= time(9, 30) and t < time(16, 0)
        
        df_rth = df[df.index.map(is_rth)]
        
        # Find previous trading day (not today)
        trading_days = sorted(set(df_rth.index.date))
        prev_days = [d for d in trading_days if d < today]
        
        if not prev_days:
            logger.warning("No previous trading day found in history")
            return None, None
        
        prev_day = prev_days[-1]
        prev_rth = df_rth[df_rth.index.date == prev_day]
        
        if len(prev_rth) == 0:
            return None, None
        
        last_candle = prev_rth.iloc[-1]
        logger.info(f"Prev close candle ({prev_day} 15:45): high={last_candle['high']} low={last_candle['low']}")
        return float(last_candle['high']), float(last_candle['low'])
        
    except Exception as e:
        logger.exception(f"Error getting prev close candle: {e}")
        return None, None


def load_secrets(path: str = "config/secrets.yaml") -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _t(hhmm: str) -> time:
    # Backwards-compatible shim (main loop uses shared util helpers now).
    return parse_hhmm(hhmm)


def main():
    settings = load_settings()
    secrets = load_secrets()

    symbol = settings["symbol"]
    market_open = settings["market_open"]
    range_end = settings["range_end"]
    be_check_time = settings["be_check_time"]
    eod_exit = settings["eod_exit"]
    target_points = float(settings["target_points"])
    buffer_points = float(settings.get("entry_buffer_points", 0.25))

    logger.add("logs/orb-trader.log", rotation="1 day", retention="14 days")

    schwab = SchwabClient(config_path="config/secrets.yaml")
    if not schwab.authenticate(interactive=True):
        raise SystemExit(1)

    campfire = notifier_from_config(settings, secrets)

    # ES point value is $50/point. If we support other symbols later, make this configurable.
    point_value = 50.0 if symbol == "/ES" else 1.0
    broker = PaperBroker(notifier=campfire, symbol=symbol, point_value=point_value)

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
    be_done = False
    eod_done = False

    skip_evaluated = False
    skip_today = False
    skip_reason = ""

    session_date = now_eastern().date()

    while True:
        now = now_eastern()
        t = now.time()

        # Daily reset (midnight ET): clear one-trade-per-day, OCO placement flags,
        # and reinitialize the ORB tracker for the new session.
        if now.date() != session_date:
            logger.info(f"New session date detected: {session_date} -> {now.date()} (resetting)")
            session_date = now.date()

            broker.reset_for_new_day(reason="NEW_DAY")
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
            be_done = False
            eod_done = False

            skip_evaluated = False
            skip_today = False
            skip_reason = ""

        # Pull quote + last price
        try:
            if symbol == "/ES":
                q = schwab.get_es_quote()
                last_price = float(q.get("price", 0.0))
                open_price = float(q.get("open", 0.0))
                prev_close = float(q.get("close", 0.0))
            else:
                q = schwab.get_quote(symbol)
                quote = q.get(symbol, {}).get("quote", {})
                last_price = float(quote.get("lastPrice", 0.0))
                open_price = float(quote.get("openPrice", 0.0))
                prev_close = float(quote.get("closePrice", 0.0))
        except Exception as e:
            logger.exception(f"Quote error: {e}")
            time_mod.sleep(5)
            continue

        # Drive ORB tracker state machine (handles seeding from history + live polling)
        state = tracker.update(now)

        # Once the range is complete, build plan + place OCO exactly once (and only before EOD)
        if (not eod_done) and (not oco_placed) and state == ORBState.RANGE_COMPLETE and (not is_past_time(now, eod_exit)):
            # Evaluate skip-day conditions once/day right before we would trade.
            if not skip_evaluated:
                # Get ORB range for overlap check
                opening_range = tracker.opening_range()
                orb_high = opening_range.high if opening_range else None
                orb_low = opening_range.low if opening_range else None
                
                # Get previous trading day's closing 15-min candle
                prev_close_high, prev_close_low = get_prev_close_candle(schwab, symbol, session_date)
                
                skip_today, skip_reason = should_skip_today(
                    day=session_date,
                    open_price=open_price,
                    prev_close=prev_close,
                    settings=settings,
                    orb_high=orb_high,
                    orb_low=orb_low,
                    prev_close_high=prev_close_high,
                    prev_close_low=prev_close_low,
                )
                skip_evaluated = True

                if skip_today:
                    logger.warning(
                        f"Skip day detected ({skip_reason}); standing down. "
                        f"open={open_price} prev_close={prev_close}"
                    )

                    # Campfire: skip-day notice (best-effort)
                    try:
                        opening_range = tracker.opening_range()
                        if opening_range is not None:
                            campfire.send_message(
                                format_skip_day(reason=skip_reason, high=opening_range.high, low=opening_range.low)
                            )
                    except Exception as e:
                        logger.error(f"Campfire skip-day alert failed: {e}")

                    oco_placed = True  # prevent retries for the rest of the session

            if (not skip_today) and (not oco_placed):
                try:
                    opening_range = tracker.opening_range()
                    if opening_range is None:
                        raise RuntimeError("ORB range complete but opening_range is None")

                    broker.place_orb_oco(
                        range_high=opening_range.high,
                        range_low=opening_range.low,
                        buffer=buffer_points,
                        target_points=target_points,
                        qty=1,
                        now=now,
                    )

                    # Campfire: range established / OCO placed (best-effort)
                    try:
                        buy_stop = float(opening_range.high + buffer_points)
                        sell_stop = float(opening_range.low - buffer_points)
                        campfire.send_message(
                            format_range_set(
                                high=opening_range.high,
                                low=opening_range.low,
                                buy_stop=buy_stop,
                                sell_stop=sell_stop,
                                range_end_time=now,
                            )
                        )
                    except Exception as e:
                        logger.error(f"Campfire range alert failed: {e}")

                    oco_placed = True
                except Exception as e:
                    logger.exception(f"Failed to finalize ORB / place OCO: {e}; will retry")

        # Drive paper broker with price updates
        broker.on_price(last_price, now=now)

        # Breakeven check at configured time (once per day)
        if (not be_done) and is_past_time(now, be_check_time):
            broker.move_stop_to_breakeven_if_in_profit(last_price, now=now)
            be_done = True

        # EOD exit (once per day): close any open position + cancel any unfilled OCO
        if (not eod_done) and is_past_time(now, eod_exit):
            broker.exit_market(last_price, reason="eod", now=now)
            logger.info("EOD exit processed; resetting for next day")

            # Clear intraday state so we're ready for the next range capture.
            broker.reset_for_new_day(reason="EOD")
            oco_placed = False
            be_done = False
            eod_done = True

        time_mod.sleep(tracker.poll_interval_s)


if __name__ == "__main__":
    main()
