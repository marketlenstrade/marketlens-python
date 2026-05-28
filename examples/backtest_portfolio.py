"""Multi-series portfolio — same strategy across ETH and SOL.

Runs one strategy against two rolling series simultaneously with shared
capital. Each fill draws from the same cash pool.
"""

from datetime import datetime, timezone

from marketlens import MarketLens
from marketlens.backtest import Strategy


class BuyOnImbalance(Strategy):
    """Buy YES when order book tilts bullish, once per market."""

    def on_market_start(self, ctx, market, book):
        self._entered = False

    def on_book(self, ctx, market, book):
        if self._entered:
            return
        imb = book.imbalance(levels=3)
        if imb is not None and imb > 0.2 and book.spread_bps() and book.spread_bps() < 500:
            ctx.buy_yes(size=100)
            self._entered = True


client = MarketLens()
result = client.backtest(
    BuyOnImbalance(),
    ["eth-up-or-down-5m", "sol-up-or-down-5m"],
    initial_cash=10_000,
    after=datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc),
    before=datetime(2026, 3, 5, 10, 5, tzinfo=timezone.utc),
)

print(result)

# Per-series attribution
for series_id, stats in result.by_series().items():
    print(f"\n  Series {series_id[:8]}:")
    print(f"    PnL: {stats['total_pnl']}  Win rate: {stats['win_rate']:.0%}"
          f"  Markets: {stats['markets_traded']}")

print(f"\n{result.settlements_df().to_string()}")
client.close()
