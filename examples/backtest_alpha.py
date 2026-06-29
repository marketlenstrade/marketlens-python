"""Alpha (signal-level) backtest — momentum tilt over a rolling series.

The signal-level model reads one bar per market per ``resolution`` from
order-book metrics (mid, spread, depth) instead of the full L2 firehose, so a
long window stays cheap. The strategy sets a target weight in ``on_bar`` and the
engine trades each market to it, filling at the next bar's mid plus slippage and
fees. State resets per market via ``on_market_start``.
"""

from datetime import datetime, timezone

from marketlens import MarketLens
from marketlens.backtest import AlphaStrategy


class MomentumTilt(AlphaStrategy):
    def on_market_start(self, ctx, market, bar):
        self._prev = None

    def on_bar(self, ctx, market, bar):
        if self._prev is not None:
            # Tilt toward YES when the mid is rising, the NO side when falling;
            # size scales with the move and is clamped to ±1 of equity.
            move = bar.mid - self._prev
            ctx.target_weight(max(-1.0, min(1.0, 50 * move)))
        self._prev = bar.mid


client = MarketLens()
result = client.backtest(
    MomentumTilt(), "solana-up-or-down-hourly",
    initial_cash=10_000,
    resolution="1m", price="mid", fill="next", slippage_bps=5,
    after=datetime(2026, 3, 5, 0, 0, tzinfo=timezone.utc),
    before=datetime(2026, 3, 6, 0, 0, tzinfo=timezone.utc),
)
print(result)
print(result.equity_df().tail().to_string())
client.close()
