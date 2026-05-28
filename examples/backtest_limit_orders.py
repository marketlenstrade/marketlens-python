"""Market-making with limit orders — post around the midpoint, exit on fill.

Uses queue_position=True for CLOB-realistic fill simulation: each limit order
tracks its position in the book queue and only fills when queue-ahead is fully
drained by trades and cancellations. For a simpler (but less accurate) model,
replace queue_position with limit_fill_rate (e.g. limit_fill_rate=0.2).
"""

from datetime import datetime, timezone

from marketlens import MarketLens
from marketlens.backtest import Strategy


class LimitTrader(Strategy):
    def on_market_start(self, ctx, market, book):
        if book.bid_levels and book.ask_levels:
            ctx.buy_yes(size=50, limit_price=book.midpoint - 0.02)

    def on_fill(self, ctx, market, fill):
        if fill.side.value.startswith("BUY"):
            ctx.sell_yes(size=fill.size, limit_price=fill.price + 0.04)


client = MarketLens()
result = client.backtest(
    LimitTrader(), "eth-up-or-down-5m",
    after=datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc),
    before=datetime(2026, 3, 5, 10, 5, tzinfo=timezone.utc),
    initial_cash=10_000,
    include_trades=True,
    queue_position=True,
)
print(result)
print(result.orders_df().to_string())
client.close()
