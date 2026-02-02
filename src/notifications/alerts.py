"""Alert message formatting for Campfire/community.

Keep all human-facing strings here so trading logic stays clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional



@dataclass(frozen=True)
class ExitSummary:
    exit_reason: str  # target|stop|breakeven_stop|eod|...
    pnl_points: float
    pnl_dollars: Optional[float]
    duration_s: float


def _side_emoji(side: str) -> str:
    return "🟢" if str(side).upper() == "LONG" else "🔴"


def _fmt_points(x: float) -> str:
    sign = "+" if x > 0 else ""  # keep minus from format
    return f"{sign}{x:.2f} pts" if abs(x) < 100 else f"{sign}{x:.0f} pts"


def _fmt_money(x: float) -> str:
    sign = "+" if x > 0 else ""  # keep minus from format
    return f"{sign}${x:,.0f}" if abs(x) >= 10 else f"{sign}${x:,.2f}"


def format_range_set(
    *,
    high: float,
    low: float,
    buy_stop: float,
    sell_stop: float,
    range_end_time: Optional[datetime] = None,
) -> str:
    rng = float(high - low)
    header = "🎯 ORB Range Set"
    if range_end_time is not None:
        header += f" ({range_end_time:%-I:%M %p} ET)"

    return (
        f"{header}\n"
        f"High: {high:.2f} | Low: {low:.2f}\n"
        f"Range: {rng:.2f} pts\n"
        f"OCO orders placed: Buy stop @ {buy_stop:.2f}, Sell stop @ {sell_stop:.2f}"
    )


def format_entry(*, side: str, entry: float, stop: float, target: float) -> str:
    side_u = str(side).upper()
    emoji = _side_emoji(side_u)

    target_pts = (target - entry) if side_u == "LONG" else (entry - target)
    stop_pts = (stop - entry) if side_u == "LONG" else (entry - stop)
    # stop_pts is typically negative if stop is beyond entry in the risk direction

    return (
        f"{emoji} {side_u} Entry @ {entry:.2f}\n"
        f"Target: {target:.2f} ({_fmt_points(target_pts)})\n"
        f"Stop: {stop:.2f} ({_fmt_points(stop_pts)})"
    )


def format_skip_day(*, reason: str, high: float, low: float) -> str:
    rng = float(high - low)
    reason_pretty = reason.replace("_", " ").title() if reason else "Skip Day"
    return (
        "⏸️ Standing down today\n"
        f"Reason: {reason_pretty}\n"
        f"Range captured: {high:.2f} - {low:.2f} ({rng:.2f} pts)"
    )


def format_exit(
    *,
    side: str,
    entry: float,
    exit_price: float,
    summary: ExitSummary,
) -> str:
    # Choose an emoji + headline by exit_reason
    reason = summary.exit_reason
    if reason == "target":
        headline = "✅ Target Hit!"
    elif reason in {"stop", "breakeven_stop"}:
        headline = "❌ Stopped Out" if reason == "stop" else "➖ Breakeven Stop"
    elif reason == "eod":
        headline = "⏰ EOD Exit"
    else:
        headline = "📤 Exit"

    pts_str = _fmt_points(summary.pnl_points)
    dollars_str = f" ({_fmt_money(summary.pnl_dollars)})" if summary.pnl_dollars is not None else ""

    mins = int(round(summary.duration_s / 60.0))
    duration_str = f"{mins} min" if mins < 120 else f"{mins/60.0:.1f} hr"

    return (
        f"{headline} {pts_str}{dollars_str}\n"
        f"Entry: {entry:.2f} → Exit: {exit_price:.2f}\n"
        f"Duration: {duration_str}"
    )
