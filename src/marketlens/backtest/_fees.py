from __future__ import annotations

from abc import ABC, abstractmethod


class FeeModel(ABC):
    @abstractmethod
    def calculate(self, price: float, size: float, is_maker: bool) -> float: ...


class PolymarketFeeModel(FeeModel):
    """Polymarket fees: ``fee = shares * p * fee_rate * (p*(1-p))^exponent``.

    Taker only (maker = 0).  Use :meth:`crypto` / :meth:`sports` presets
    or :meth:`for_category` to auto-detect from a market's category.
    """

    def __init__(self, fee_rate: float, exponent: int = 1) -> None:
        self._fee_rate = fee_rate
        self._exponent = exponent

    @classmethod
    def crypto(cls) -> PolymarketFeeModel:
        """Crypto markets: fee_rate=0.25, exponent=2. Max ~1.56% at p=0.50."""
        return cls(0.25, exponent=2)

    @classmethod
    def sports(cls) -> PolymarketFeeModel:
        """Sports markets (NCAAB, Serie A): fee_rate=0.0175, exponent=1. Max ~0.44% at p=0.50."""
        return cls(0.0175, exponent=1)

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

    def calculate(self, price: float, size: float, is_maker: bool) -> float:
        if is_maker:
            return 0.0
        fee_per_share = price * self._fee_rate * (price * (1.0 - price)) ** self._exponent
        return fee_per_share * size


class ZeroFeeModel(FeeModel):
    """Always returns 0."""

    def calculate(self, price: float, size: float, is_maker: bool) -> float:
        return 0.0


class FlatFeeModel(FeeModel):
    """Fixed fee per share."""

    def __init__(self, fee_per_share: float) -> None:
        self._fee = fee_per_share

    def calculate(self, price: float, size: float, is_maker: bool) -> float:
        return self._fee * size
