"""Price/tick utilities.

Futures like /ES trade in fixed tick increments (0.25 points). Broker APIs will
reject prices that are not on a valid tick.
"""

from __future__ import annotations

import math


def round_to_tick(price: float, tick_size: float = 0.25, direction: str = "nearest") -> float:
    """Round a price to a valid tick increment.

    Args:
        price: Raw price.
        tick_size: Minimum price increment.
        direction: "down" | "up" | "nearest".

    Returns:
        Rounded price.
    """
    if tick_size <= 0:
        raise ValueError("tick_size must be > 0")

    # 0.25 is exactly representable in binary, but we still guard with a tiny
    # epsilon to avoid edge cases when price comes from chained float ops.
    eps = 1e-12

    if direction == "down":
        q = (price + eps) / tick_size
        v = math.floor(q) * tick_size
    elif direction == "up":
        q = (price - eps) / tick_size
        v = math.ceil(q) * tick_size
    else:
        q = price / tick_size
        v = round(q) * tick_size

    return float(v)


def es_tick_size(symbol: str) -> float:
    """Return tick size for /ES (and micro /MES) style symbols."""
    if symbol in {"/ES", "/MES"}:
        return 0.25
    # Default (unknown) tick size.
    return 0.01
