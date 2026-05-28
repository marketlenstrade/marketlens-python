"""Surface mispricing strategy on a structured product.

Fits an isotonic survival curve from all sibling strike midpoints at every
tick. When the fitted probability exceeds the raw midpoint by a threshold,
the strike is underpriced relative to the curve — buy YES.

Uses on_market_start to collect market metadata, ctx.books for cross-strike
state, compute_surface for real-time PAVA regression, and ctx.reference_price()
to filter trades to strikes near the current spot price.
"""

from datetime import datetime, timezone

from marketlens import MarketLens
from marketlens.backtest import Strategy
from marketlens.helpers.surface import compute_surface


class SurfaceMispricing(Strategy):
    def __init__(self, series, edge=0.02):
        self.series = series
        self.edge = edge
        self._mkts: dict = {}
        self._traded: set = set()

    def on_market_start(self, ctx, market, book):
        self._mkts[market.id] = market

    def on_book(self, ctx, market, book):
        if market.id in self._traded:
            return
        # Only trade strikes near the current spot price
        ref = ctx.reference_price()
        if ref and market.strike:
            if abs(ref - market.strike) / ref > 0.05:
                return
        surface = compute_surface(ctx.books, self._mkts, self.series)
        if not surface:
            return
        for s in surface.survival_strikes():
            if s.market_id == market.id and s.fitted_prob - s.raw_prob > self.edge:
                ctx.buy_yes(size=100)
                self._traded.add(market.id)
                break


client = MarketLens()
series = client.series.get("ethereum-multi-strikes-weekly")
result = client.backtest(
    SurfaceMispricing(series, edge=0.01), "ethereum-multi-strikes-weekly",
    initial_cash=10_000,
    after=datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc),
    before=datetime(2026, 3, 5, 10, 5, tzinfo=timezone.utc),
)
print(result)
print(result.trades_df().to_string())
print(result.settlements_df().to_string())
client.close()
