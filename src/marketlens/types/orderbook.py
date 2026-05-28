from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from marketlens.types._validators import none_to_half, none_to_zero


class PriceLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: float
    size: float


class OrderBook(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_id: str
    platform: str
    as_of: int
    bids: list[PriceLevel]
    asks: list[PriceLevel]
    best_bid: float = 0.5
    best_ask: float = 0.5
    spread: float = 0.0
    midpoint: float = 0.5
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    bid_levels: int
    ask_levels: int

    _coerce_price = none_to_half("best_bid", "best_ask", "midpoint")
    _coerce_size = none_to_zero("spread", "bid_depth", "ask_depth")

    def impact(self, side: str, size: float) -> float | None:
        """Volume-weighted average execution price for a hypothetical market order.

        Args:
            side: "BUY" or "SELL".
            size: Order size in USD.

        Returns:
            Average execution price, or None if insufficient liquidity.
        """
        remaining = size
        levels = self.asks if side == "BUY" else self.bids
        total_cost = 0.0
        total_filled = 0.0

        for level in levels:
            fill = min(remaining, level.size)
            total_cost += fill * level.price
            total_filled += fill
            remaining -= fill
            if remaining <= 0:
                break

        if total_filled == 0:
            return None
        return total_cost / total_filled

    def depth_within(self, spread: float) -> tuple[float, float]:
        """Total size on each side within ``spread`` of midpoint.

        Args:
            spread: Maximum distance from midpoint.

        Returns:
            ``(bid_depth, ask_depth)``.
        """
        if not self.bids or not self.asks:
            return (0.0, 0.0)

        # Half-tick (5e-5) tolerance keeps the inclusive boundary stable
        # against float subtraction artefacts (e.g. 0.66-0.65 → 0.01000...09).
        threshold = spread + 5e-5
        bid_total = sum(level.size for level in self.bids if self.midpoint - level.price <= threshold)
        ask_total = sum(level.size for level in self.asks if level.price - self.midpoint <= threshold)
        return (bid_total, ask_total)

    def slippage(self, side: str, size: float) -> float | None:
        """Difference between midpoint and average execution price.

        Args:
            side: "BUY" or "SELL".
            size: Order size in USD.

        Returns:
            Slippage (always non-negative), or None if either side of the
            book is empty.
        """
        if not self.bids or not self.asks:
            return None
        avg = self.impact(side, size)
        if avg is None:
            return None
        return abs(avg - self.midpoint)

    def microprice(self) -> float | None:
        """Size-weighted midpoint from the top-of-book (alias for ``weighted_midpoint(1)``).

        This is the canonical "microprice" from microstructure literature —
        the best-level weighted mid that adjusts for queue imbalance.

        Returns:
            Microprice, or ``None`` if either side has no levels.
        """
        return self.weighted_midpoint(1)

    def spread_bps(self) -> float | None:
        """Spread expressed in basis points relative to midpoint.

        ``spread / midpoint * 10_000``.

        Returns:
            Spread in bps, or ``None`` if either side of the book is empty.
        """
        if not self.bids or not self.asks:
            return None
        return self.spread / self.midpoint * 10_000

    def imbalance(self, levels: int | None = None) -> float | None:
        """Order book imbalance: ``(bid_depth - ask_depth) / (bid_depth + ask_depth)``.

        Args:
            levels: Number of top levels to include from each side.
                When ``None`` (default), uses the full book depth.

        Returns a float in ``[-1, 1]``, or ``None`` if the book is empty.
        A positive value indicates more resting liquidity on the bid side.
        """
        if levels is None:
            bd, ad = self.bid_depth, self.ask_depth
        else:
            bd = sum(l.size for l in self.bids[:levels])
            ad = sum(l.size for l in self.asks[:levels])
        total = bd + ad
        if total == 0:
            return None
        return (bd - ad) / total

    def weighted_midpoint(self, n: int = 1) -> float | None:
        """Size-weighted midpoint from the top *n* levels on each side.

        More responsive than the simple midpoint when the best level has
        thin liquidity.  With ``n=1`` this is the classic weighted mid::

            wmid = (best_bid * ask_size + best_ask * bid_size)
                 / (bid_size + ask_size)

        Args:
            n: Number of top levels to include from each side.

        Returns:
            Weighted midpoint, or ``None`` if either side has no levels.
        """
        top_bids = self.bids[:n]
        top_asks = self.asks[:n]
        if not top_bids or not top_asks:
            return None

        bid_value = sum(l.price * l.size for l in top_bids)
        bid_size = sum(l.size for l in top_bids)
        ask_value = sum(l.price * l.size for l in top_asks)
        ask_size = sum(l.size for l in top_asks)

        total_size = bid_size + ask_size
        if total_size == 0:
            return None

        return (bid_value / bid_size * ask_size + ask_value / ask_size * bid_size) / total_size


class BookMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    t: int
    best_bid: float = 0.5
    best_ask: float = 0.5
    spread: float = 0.0
    midpoint: float = 0.5
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    bid_levels: int
    ask_levels: int

    _coerce_price = none_to_half("best_bid", "best_ask", "midpoint")
    _coerce_size = none_to_zero("spread", "bid_depth", "ask_depth")
