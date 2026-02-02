#!/usr/bin/env python3
"""Backtest the /ES Opening Range Breakout (ORB) strategy.

This script:
- Pulls 15-minute /ES candles from Schwab (RTH only)
- Builds the opening range from the 09:30–09:45 candle
- Applies skip-day logic (FOMC, gap-fill, range overlap)
- Simulates an OCO breakout entry at ORB boundaries
- Uses fixed +20pt targets and stop rules per `src/strategy/orb.py`
- Moves stop to breakeven at 10:00 ET if still in a trade
- Exits any open trade at 16:00 ET (EOD)

Outputs:
- Console summary
- logs/backtest_results.json with full per-day details

Notes:
- $/ES point value = $50
- All times are America/New_York
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pytz
import yaml
from loguru import logger

# Allow running as a script from the repo root without installing as a package.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data.schwab import SchwabClient
from src.strategy.orb import OpeningRange, build_orb_plan
from src.strategy.skip_days import should_skip_today


ES_POINT_VALUE = 50.0
TZ = "America/New_York"


def _eastern() -> pytz.BaseTzInfo:
    return pytz.timezone(TZ)


def _to_eastern_index(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    if out.index.tz is None:
        # Schwab history typically arrives as epoch ms => UTC but tz-naive.
        out.index = out.index.tz_localize(pytz.UTC)
    out.index = out.index.tz_convert(_eastern())
    return out


def _session_window(d: date) -> Tuple[datetime, datetime]:
    eastern = _eastern()
    start = eastern.localize(datetime.combine(d, time(9, 30)))
    end = eastern.localize(datetime.combine(d, time(16, 0)))
    return start, end


def _load_settings(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def _schwab_price_history_15m(
    schwab: SchwabClient,
    symbol: str,
    start_e: datetime,
    end_e: datetime,
) -> pd.DataFrame:
    if not schwab.client:
        raise RuntimeError("Schwab client not authenticated")

    # Schwab endpoint expects timezone-aware datetimes (passed through schwab-py).
    start_utc = start_e.astimezone(pytz.UTC)
    end_utc = end_e.astimezone(pytz.UTC)

    resp = schwab.client.get_price_history(
        symbol=symbol,
        period_type=schwab.client.PriceHistory.PeriodType.DAY,
        period=schwab.client.PriceHistory.Period.ONE_DAY,  # ignored when start/end provided
        frequency_type=schwab.client.PriceHistory.FrequencyType.MINUTE,
        frequency=schwab.client.PriceHistory.Frequency.EVERY_FIFTEEN_MINUTES,
        start_datetime=start_utc,
        end_datetime=end_utc,
        need_extended_hours_data=False,
        need_previous_close=False,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Schwab history failed: {resp.status_code} {resp.text}")

    payload = resp.json() or {}
    candles = payload.get("candles", [])
    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ms")
    df = df.set_index("datetime")
    return df[["open", "high", "low", "close", "volume"]]


def _rth_only_15m(df_e: pd.DataFrame) -> pd.DataFrame:
    if df_e.empty:
        return df_e

    # Keep only candles whose start timestamps are within [09:30, 16:00).
    idx = df_e.index
    mask = (
        (idx.time >= time(9, 30))
        & (idx.time < time(16, 0))
    )
    return df_e.loc[mask].copy()


def _get_last_trading_dates(df_e_rth: pd.DataFrame, n: int) -> List[date]:
    if df_e_rth.empty:
        return []

    dates = sorted({ts.date() for ts in df_e_rth.index})
    return dates[-n:]


def _candle_at(df_day: pd.DataFrame, d: date, hhmm: str) -> Optional[pd.Series]:
    eastern = _eastern()
    t = time.fromisoformat(hhmm)
    ts = eastern.localize(datetime.combine(d, t))
    if ts in df_day.index:
        return df_day.loc[ts]
    return None


def _simulate_day(
    d: date,
    df_day: pd.DataFrame,
    prev_day: Optional[date],
    df_prev: Optional[pd.DataFrame],
    settings: dict,
    symbol: str,
) -> Dict[str, Any]:
    """Simulate one RTH day using 15-minute candles."""

    entry_buffer = float(settings.get("entry_buffer_points", 0.0) or 0.0)
    target_points = float(settings.get("target_points", 20.0) or 20.0)

    # Required candles
    orb_candle = _candle_at(df_day, d, "09:30")
    prev_close_candle = None
    if prev_day and df_prev is not None and not df_prev.empty:
        prev_close_candle = _candle_at(df_prev, prev_day, "15:45")

    if orb_candle is None:
        return {
            "date": d.isoformat(),
            "skipped": True,
            "skip_reason": "MISSING_ORB_CANDLE",
        }

    orb_high = float(orb_candle["high"])
    orb_low = float(orb_candle["low"])
    open_price = float(orb_candle["open"])

    prev_close = float(prev_close_candle["close"]) if prev_close_candle is not None else None
    prev_close_high = float(prev_close_candle["high"]) if prev_close_candle is not None else None
    prev_close_low = float(prev_close_candle["low"]) if prev_close_candle is not None else None

    skip, reason = should_skip_today(
        day=d,
        open_price=open_price,
        prev_close=prev_close,
        settings=settings,
        orb_high=orb_high,
        orb_low=orb_low,
        prev_close_high=prev_close_high,
        prev_close_low=prev_close_low,
    )
    if skip:
        return {
            "date": d.isoformat(),
            "skipped": True,
            "skip_reason": reason,
            "orb": {"high": orb_high, "low": orb_low, "open": open_price},
            "prev_close": prev_close,
        }

    eastern = _eastern()
    orb_start, _ = _session_window(d)
    opening_range = OpeningRange(
        start=orb_start,
        end=orb_start + timedelta(minutes=15),
        high=orb_high,
        low=orb_low,
    )

    plan = build_orb_plan(symbol=symbol, opening_range=opening_range, target_points=target_points)

    long_entry = float(plan.long_entry + entry_buffer)
    short_entry = float(plan.short_entry - entry_buffer)

    long_target = float(long_entry + target_points)
    short_target = float(short_entry - target_points)

    long_stop = float(plan.long_stop)
    short_stop = float(plan.short_stop)

    be_time = eastern.localize(datetime.combine(d, time.fromisoformat(settings.get("be_check_time", "10:00"))))

    # Iterate candles after opening range completes.
    df_after = df_day.loc[df_day.index >= eastern.localize(datetime.combine(d, time(9, 45)))].copy()

    in_pos = False
    side = None  # 'LONG'|'SHORT'
    entry_ts = None
    entry_px = None
    exit_ts = None
    exit_px = None
    exit_reason = None  # TARGET|STOP|EOD|AMBIGUOUS

    stop_px = None
    target_px = None

    for ts, candle in df_after.iterrows():
        o, h, l, c = map(float, (candle["open"], candle["high"], candle["low"], candle["close"]))

        # Breakeven move at 10:00 (first candle starting at 10:00)
        if in_pos and ts == be_time:
            if side == "LONG":
                stop_px = max(float(stop_px), float(entry_px))
            elif side == "SHORT":
                stop_px = min(float(stop_px), float(entry_px))

        if not in_pos:
            long_hit = h >= long_entry
            short_hit = l <= short_entry

            if long_hit and short_hit:
                # Ambiguous in 15m OHLC. Treat as no-trade to avoid overstating results.
                exit_reason = "AMBIGUOUS_BOTH_BREAKOUTS"
                break

            if long_hit:
                in_pos = True
                side = "LONG"
                entry_ts = ts
                entry_px = long_entry
                stop_px = long_stop
                target_px = long_target
                continue

            if short_hit:
                in_pos = True
                side = "SHORT"
                entry_ts = ts
                entry_px = short_entry
                stop_px = short_stop
                target_px = short_target
                continue

            continue

        # Manage open position
        if side == "LONG":
            stop_hit = l <= float(stop_px)
            target_hit = h >= float(target_px)
            if stop_hit and target_hit:
                # Conservative: assume stop first.
                exit_ts = ts
                exit_px = float(stop_px)
                exit_reason = "STOP_AND_TARGET_SAME_CANDLE"
                in_pos = False
                break
            if stop_hit:
                exit_ts = ts
                exit_px = float(stop_px)
                exit_reason = "STOP"
                in_pos = False
                break
            if target_hit:
                exit_ts = ts
                exit_px = float(target_px)
                exit_reason = "TARGET"
                in_pos = False
                break

        elif side == "SHORT":
            stop_hit = h >= float(stop_px)
            target_hit = l <= float(target_px)
            if stop_hit and target_hit:
                exit_ts = ts
                exit_px = float(stop_px)
                exit_reason = "STOP_AND_TARGET_SAME_CANDLE"
                in_pos = False
                break
            if stop_hit:
                exit_ts = ts
                exit_px = float(stop_px)
                exit_reason = "STOP"
                in_pos = False
                break
            if target_hit:
                exit_ts = ts
                exit_px = float(target_px)
                exit_reason = "TARGET"
                in_pos = False
                break

    # If ambiguous before entry
    if exit_reason == "AMBIGUOUS_BOTH_BREAKOUTS":
        return {
            "date": d.isoformat(),
            "skipped": True,
            "skip_reason": exit_reason,
            "orb": {"high": orb_high, "low": orb_low, "open": open_price},
            "prev_close": prev_close,
            "plan": {
                "long_entry": long_entry,
                "short_entry": short_entry,
                "long_stop": long_stop,
                "short_stop": short_stop,
                "long_target": long_target,
                "short_target": short_target,
            },
        }

    # EOD exit if still in position
    if in_pos:
        last_ts = df_after.index.max()
        last_close = float(df_after.loc[last_ts]["close"]) if last_ts is not None else None
        exit_ts = last_ts
        exit_px = last_close
        exit_reason = "EOD"

    traded = entry_ts is not None and exit_ts is not None and entry_px is not None and exit_px is not None

    points = 0.0
    if traded:
        if side == "LONG":
            points = float(exit_px - entry_px)
        else:
            points = float(entry_px - exit_px)

    dollars = float(points * ES_POINT_VALUE)

    return {
        "date": d.isoformat(),
        "skipped": False,
        "skip_reason": "",
        "orb": {"high": orb_high, "low": orb_low, "open": open_price},
        "prev_close": prev_close,
        "plan": {
            "long_entry": long_entry,
            "short_entry": short_entry,
            "long_stop": long_stop,
            "short_stop": short_stop,
            "long_target": long_target,
            "short_target": short_target,
        },
        "trade": {
            "side": side,
            "entry_time": entry_ts.isoformat() if entry_ts is not None else None,
            "entry_price": entry_px,
            "exit_time": exit_ts.isoformat() if exit_ts is not None else None,
            "exit_price": exit_px,
            "exit_reason": exit_reason,
            "points": points,
            "dollars": dollars,
        },
    }


def _summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_days = len(results)
    skipped = [r for r in results if r.get("skipped")]
    traded = [r for r in results if not r.get("skipped") and r.get("trade") and r["trade"].get("side")]

    skip_reasons = Counter(r.get("skip_reason") or "" for r in skipped)
    if "" in skip_reasons:
        del skip_reasons[""]

    wins: List[float] = []
    losses: List[float] = []
    breakevens: List[float] = []

    for r in traded:
        p = float(r["trade"]["points"])
        if p > 0:
            wins.append(p)
        elif p < 0:
            losses.append(p)
        else:
            breakevens.append(p)

    gross_win = float(sum(wins))
    gross_loss = float(sum(abs(x) for x in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    total_points = float(sum(float(r["trade"]["points"]) for r in traded))
    total_dollars = float(total_points * ES_POINT_VALUE)

    denom = (len(wins) + len(losses))
    win_rate = (len(wins) / denom * 100.0) if denom > 0 else 0.0

    avg_win_pts = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss_pts = (sum(losses) / len(losses)) if losses else 0.0

    largest_win = max(wins) if wins else 0.0
    largest_loss = min(losses) if losses else 0.0

    summary = {
        "total_days": total_days,
        "traded_days": len(traded),
        "skipped_days": len(skipped),
        "skip_reasons": dict(skip_reasons),
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": len(breakevens),
        "win_rate_pct": win_rate,
        "avg_win_points": avg_win_pts,
        "avg_win_dollars": avg_win_pts * ES_POINT_VALUE,
        "avg_loss_points": avg_loss_pts,
        "avg_loss_dollars": avg_loss_pts * ES_POINT_VALUE,
        "profit_factor": profit_factor,
        "total_points": total_points,
        "total_dollars": total_dollars,
        "largest_win_points": largest_win,
        "largest_win_dollars": largest_win * ES_POINT_VALUE,
        "largest_loss_points": largest_loss,
        "largest_loss_dollars": largest_loss * ES_POINT_VALUE,
    }
    return summary


def _print_summary(summary: Dict[str, Any]) -> None:
    pf = summary.get("profit_factor")
    pf_str = f"{pf:.2f}" if pf is not None else "N/A"

    lines = [
        "\n================ ORB Backtest Summary ================",
        f"Total days:      {summary['total_days']}",
        f"Traded days:     {summary['traded_days']}",
        f"Skipped days:    {summary['skipped_days']}",
        "",
        f"Wins / Losses / BE: {summary['wins']} / {summary['losses']} / {summary.get('breakevens', 0)}",
        f"Win rate:           {summary['win_rate_pct']:.1f}% (excl. breakevens)",
        "",
        f"Avg win:         {summary['avg_win_points']:.2f} pts  (${summary['avg_win_dollars']:.2f})",
        f"Avg loss:        {summary['avg_loss_points']:.2f} pts  (${summary['avg_loss_dollars']:.2f})",
        f"Profit factor:   {pf_str}",
        "",
        f"Total P&L:       {summary['total_points']:.2f} pts  (${summary['total_dollars']:.2f})",
        f"Largest win:     {summary['largest_win_points']:.2f} pts  (${summary['largest_win_dollars']:.2f})",
        f"Largest loss:    {summary['largest_loss_points']:.2f} pts  (${summary['largest_loss_dollars']:.2f})",
        "",
        "Skip reasons:",
    ]

    if summary["skip_reasons"]:
        for k, v in sorted(summary["skip_reasons"].items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  - {k}: {v}")
    else:
        lines.append("  (none)")

    lines.append("======================================================\n")
    print("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest ORB strategy on /ES using Schwab 15m candles")
    ap.add_argument("--days", type=int, default=30, help="Number of most recent trading days")
    ap.add_argument("--symbol", type=str, default="/ES", help="Symbol to backtest (default /ES)")
    ap.add_argument("--settings", type=str, default="config/settings.yaml", help="Settings YAML")
    ap.add_argument("--interactive-auth", action="store_true", help="Force interactive Schwab auth")
    ap.add_argument("--out", type=str, default="logs/backtest_results.json", help="Output JSON path")
    args = ap.parse_args()

    settings = _load_settings(args.settings)

    schwab = SchwabClient()
    if args.interactive_auth:
        ok = schwab.authenticate(interactive=True)
    else:
        ok = schwab.authenticate(interactive=False) or schwab.authenticate(interactive=True)
    if not ok:
        raise SystemExit("Schwab authentication failed")

    # Pull a wider window than needed so we can reliably extract last N trading days.
    end_e = _eastern().localize(datetime.now())
    start_e = end_e - timedelta(days=70)

    logger.info(f"Fetching 15m history for {args.symbol} from {start_e} to {end_e}")
    raw = _schwab_price_history_15m(schwab, args.symbol, start_e=start_e, end_e=end_e)
    df_e = _to_eastern_index(raw)
    df_rth = _rth_only_15m(df_e)

    dates = _get_last_trading_dates(df_rth, args.days)
    if not dates:
        raise SystemExit("No RTH candles returned; cannot backtest")

    results: List[Dict[str, Any]] = []

    for i, d in enumerate(dates):
        start_s, end_s = _session_window(d)
        df_day = df_rth.loc[(df_rth.index >= start_s) & (df_rth.index < end_s)].copy()

        prev_day = dates[i - 1] if i > 0 else None
        df_prev = None
        if prev_day is not None:
            prev_start, prev_end = _session_window(prev_day)
            df_prev = df_rth.loc[(df_rth.index >= prev_start) & (df_rth.index < prev_end)].copy()

        res = _simulate_day(
            d=d,
            df_day=df_day,
            prev_day=prev_day,
            df_prev=df_prev,
            settings=settings,
            symbol=args.symbol,
        )
        results.append(res)

    summary = _summarize(results)
    _print_summary(summary)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": datetime.now(tz=_eastern()).isoformat(),
        "symbol": args.symbol,
        "days": args.days,
        "tz": TZ,
        "point_value": ES_POINT_VALUE,
        "settings": settings,
        "summary": summary,
        "results": results,
    }

    out_path.write_text(json.dumps(payload, indent=2, sort_keys=False))
    print(f"Saved detailed results to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
