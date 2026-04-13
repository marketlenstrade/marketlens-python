from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

_FOUR = Decimal("0.0001")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class FeeModel(ABC):
    @abstractmethod
    def calculate(self, price: Decimal, size: Decimal, is_maker: bool) -> Decimal: ...


class PolymarketFeeModel(FeeModel):
    """Polymarket fees: ``fee = shares * p * fee_rate * (p*(1-p))^exponent``.

    Taker only (maker = 0).  Use :meth:`crypto` / :meth:`sports` presets
    or :meth:`for_category` to auto-detect from a market's category.
    """

    def __init__(self, fee_rate: Decimal, exponent: int = 1) -> None:
        self._fee_rate = fee_rate
        self._exponent = exponent

    @classmethod
    def crypto(cls) -> PolymarketFeeModel:
        """Crypto markets: fee_rate=0.25, exponent=2. Max ~1.56% at p=0.50."""
        return cls(Decimal("0.25"), exponent=2)

    @classmethod
    def sports(cls) -> PolymarketFeeModel:
        """Sports markets (NCAAB, Serie A): fee_rate=0.0175, exponent=1. Max ~0.44% at p=0.50."""
        return cls(Decimal("0.0175"), exponent=1)

    _SPORTS_CATEGORIES = frozenset({
        "sports", "football", "basketball", "baseball", "hockey",
        "soccer", "tennis", "golf", "mma", "boxing", "cricket",
        "rugby", "nfl", "nba", "mlb", "nhl", "ncaab",
    })

    @classmethod
    def for_category(cls, category: str | None) -> FeeModel:
        """Return the correct fee model for a Polymarket market category."""
        if not category:
            return ZeroFeeModel()
        cat = category.lower()
        if cat == "crypto":
            return cls.crypto()
        if cat in cls._SPORTS_CATEGORIES:
            return cls.sports()
        return ZeroFeeModel()

    def calculate(self, price: Decimal, size: Decimal, is_maker: bool) -> Decimal:
        if is_maker:
            return _ZERO
        fee_per_share = (
            price * self._fee_rate * (price * (_ONE - price)) ** self._exponent
        )
        return (fee_per_share * size).quantize(_FOUR)


class ZeroFeeModel(FeeModel):
    """Always returns 0."""

    def calculate(self, price: Decimal, size: Decimal, is_maker: bool) -> Decimal:
        return _ZERO


class FlatFeeModel(FeeModel):
    """Fixed fee per share."""

    def __init__(self, fee_per_share: Decimal) -> None:
        self._fee = fee_per_share

    def calculate(self, price: Decimal, size: Decimal, is_maker: bool) -> Decimal:
        return (self._fee * size).quantize(_FOUR)
