"""Shared decimal constants and rounding helpers for Polymarket strategies."""

from __future__ import annotations

from decimal import Decimal

TICK_PRICE = Decimal("0.01")       # Polymarket minimum price increment
TICK_SHARES = Decimal("0.0001")    # share / position precision


def round_price(value: Decimal | str | float) -> Decimal:
    """Quantize a value to Polymarket price precision (2 decimal places)."""
    return Decimal(str(value)).quantize(TICK_PRICE)


def round_shares(value: Decimal | str | float) -> Decimal:
    """Quantize a value to share precision (4 decimal places)."""
    return Decimal(str(value)).quantize(TICK_SHARES)
