"""Skip-day detection helpers.

The ORB strategy should avoid trading on certain days/conditions:
- FOMC meeting days (configured list)
- Large overnight gaps where price tends to mean-revert / fill the gap first

This module keeps the logic isolated so it can be unit tested and evolved.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Tuple


def _as_date(d: Any) -> date:
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        # Accept ISO date strings (YYYY-MM-DD)
        return datetime.fromisoformat(d).date()
    raise TypeError(f"Unsupported date type: {type(d)!r}")


def is_fomc_day(day: date, fomc_dates: Iterable[str] | None = None) -> bool:
    """Return True if `day` is in the configured list of FOMC dates."""
    if not fomc_dates:
        return False

    d = _as_date(day)
    iso = d.isoformat()
    return any(str(x) == iso for x in fomc_dates)


def is_range_overlap_day(
    orb_high: float,
    orb_low: float,
    prev_close_high: float,
    prev_close_low: float,
) -> bool:
    """Return True if today's ORB overlaps with yesterday's closing candle range.
    
    Per the ebook: If today's opening 15-min candle trades within the range of
    yesterday's closing 15-min candle, skip the day.
    
    Overlap = any intersection between the two ranges.
    """
    if any(v is None or v <= 0 for v in [orb_high, orb_low, prev_close_high, prev_close_low]):
        return False
    
    # No overlap if ORB is completely above or below prev close range
    no_overlap = orb_high < prev_close_low or orb_low > prev_close_high
    return not no_overlap


def is_wide_range_day(range_size: float, max_range_points: float = 38.0) -> bool:
    """Return True if the ORB range exceeds the maximum allowed size.
    
    Per Jon: If the 9:30-9:45 range is greater than 38 points, skip the day.
    Wide ranges = high volatility = unpredictable breakouts.
    """
    if range_size is None or range_size <= 0:
        return False
    return range_size > max_range_points


def is_gap_fill_day(open_price: float, prev_close: float, threshold_pct: float) -> bool:
    """Return True if the overnight gap exceeds `threshold_pct`.

    gap_pct = abs(open - prev_close) / prev_close * 100

    Notes:
    - If `prev_close` is 0 or missing, we cannot compute a meaningful gap.
    """
    if prev_close is None or prev_close <= 0:
        return False
    if open_price is None or open_price <= 0:
        return False

    gap_pct = abs(float(open_price) - float(prev_close)) / float(prev_close) * 100.0
    return gap_pct > float(threshold_pct)


def should_skip_today(
    day: date,
    open_price: float,
    prev_close: float,
    settings: dict,
    orb_high: float = None,
    orb_low: float = None,
    prev_close_high: float = None,
    prev_close_low: float = None,
) -> Tuple[bool, str]:
    """Evaluate all skip-day conditions.

    Returns:
        (skip, reason)

    Reason is a short stable string suitable for logs.
    
    Skip conditions:
    1. FOMC day (from config list)
    2. Gap-fill day (overnight gap > threshold)
    3. Range overlap day (ORB overlaps prev close candle)
    4. Wide range day (ORB > 38 points)
    """

    cfg = (settings or {}).get("skip_days", {}) if isinstance(settings, dict) else {}

    fomc_dates = cfg.get("fomc_dates_2026") or cfg.get("fomc_dates")
    if is_fomc_day(day, fomc_dates=fomc_dates):
        return True, "FOMC_DAY"

    # Backwards-compatible: allow threshold at either settings.skip_days.gap_threshold_pct
    # or top-level settings.gap_threshold_pct.
    threshold_pct = cfg.get("gap_threshold_pct")
    if threshold_pct is None:
        threshold_pct = (settings or {}).get("gap_threshold_pct")

    if threshold_pct is not None and is_gap_fill_day(open_price, prev_close, float(threshold_pct)):
        return True, "GAP_FILL_DAY"

    # Range overlap check (ebook rule: ORB within prev close candle = skip)
    if all(v is not None for v in [orb_high, orb_low, prev_close_high, prev_close_low]):
        if is_range_overlap_day(orb_high, orb_low, prev_close_high, prev_close_low):
            return True, "RANGE_OVERLAP_DAY"

    # Wide range check: ORB > 38 points = too volatile
    if orb_high is not None and orb_low is not None:
        range_size = orb_high - orb_low
        max_range = cfg.get("max_range_points", 38.0)
        if is_wide_range_day(range_size, max_range):
            return True, "WIDE_RANGE_DAY"

    return False, ""
