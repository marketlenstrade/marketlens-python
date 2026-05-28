"""Basic backtest — spread-timing strategy on a rolling series.

The engine walks sequential markets in the series, settling each before
moving to the next. Strategy state resets per market via on_market_start.
"""

from datetime import datetime, timezone

from marketlens import MarketLens
from marketlens.backtest import Strategy


class SpreadTimer(Strategy):
    def on_market_start(self, ctx, market, book):
        self._entered = False

    def on_book(self, ctx, market, book):
        if self._entered:
            return
        if (s := book.spread_bps()) and s < 300 and book.imbalance(levels=3) > 0.1:
            ctx.buy_yes(size=200)
            self._entered = True


client = MarketLens()
result = client.backtest(
    SpreadTimer(), "solana-up-or-down-hourly",
    initial_cash=10_000,
    after=datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc),
    before=datetime(2026, 3, 5, 10, 5, tzinfo=timezone.utc),
)
print(result)
print(result.trades_df().to_string())
client.close()
