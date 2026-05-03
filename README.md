# marketlens

Backtest prediction market strategies on tick-level L2 order book data from Polymarket.

```bash
pip install marketlens
```

## Backtest

Define a strategy, run it against any market or series — the engine replays full L2 book state tick-by-tick with realistic execution.

```python
from marketlens import MarketLens
from marketlens.backtest import Strategy

class OpeningFader(Strategy):
    def on_market_start(self, ctx, market, book):
        self._entered = False

    def on_book(self, ctx, market, book):
        if self._entered or book.midpoint is None:
            return
        if float(book.midpoint) < 0.50:
            ctx.buy_yes(size="200")
        else:
            ctx.buy_no(size="200")
        self._entered = True

client = MarketLens()  # uses MARKETLENS_API_KEY env var
result = client.backtest(
    OpeningFader(), "btc-up-or-down-5m",
    initial_cash="10000",
    after="2026-04-15T01:45:00Z", before="2026-04-15T02:00:00Z",
)
print(result.summary())
```

Pass a market ID, series slug, or a list of series for multi-asset portfolios:

Always pass `after`/`before` — series and multi-strike runs are otherwise unbounded.

```python
# Single market — replays the full lifetime of the market by default
result = client.backtest(strategy, market_id, initial_cash="10000")

# Rolling series — walks every market in [after, before)
result = client.backtest(strategy, "btc-up-or-down-5m", initial_cash="10000",
                         after="2026-04-15T01:45:00Z",
                         before="2026-04-15T02:00:00Z")

# Multi-asset portfolio — shared capital across series
result = client.backtest(strategy,
    ["btc-up-or-down-5m", "eth-up-or-down-5m", "sol-up-or-down-5m"],
    initial_cash="10000",
    after="2026-04-15T01:45:00Z", before="2026-04-15T02:00:00Z")

# Structured product — replays every strike market in the matched event(s).
# Pass `after` to pick a single recent event; events are typically week-long,
# so a wide window can pull millions of book events.
result = client.backtest(strategy, "btc-multi-strikes-weekly",
                         initial_cash="10000",
                         after="2026-05-08T00:00:00Z")
```

### Execution realism

| Parameter | Default | Description |
|-----------|---------|-------------|
| `latency_ms` | `50` | Order-to-fill delay in milliseconds |
| `queue_position` | `False` | CLOB queue modeling — fills only when queue-ahead is drained by trades |
| `limit_fill_rate` | `0.1` | Fraction of trade size filling your limit (ignored when `queue_position=True`) |
| `slippage_bps` | `0` | Extra price penalty on market order fills |
| `fees` | `"polymarket"` | Auto-detects crypto vs sports fee schedule; `None` for zero fees |
| `max_fill_fraction` | `1.0` | Max fraction of each book level consumed per order |
| `include_trades` | `True` | Fetch trade data (required for limit fills and `on_trade`) |
| `settlement_delay_ms` | `5000` | Delay before filled tokens become sellable (on-chain settlement) |

The portfolio automatically handles **CTF merge** (opposite-side netting): buying NO while holding YES nets matched pairs at $1 per share. No explicit merge call needed in backtests.

### Strategy hooks

| Hook | Called when |
|------|------------|
| `on_book(ctx, market, book)` | Every book state change (snapshot or delta) |
| `on_trade(ctx, market, book, trade)` | Every executed trade |
| `on_fill(ctx, market, fill)` | Your order is filled |
| `on_market_start(ctx, market, book)` | A new market begins |
| `on_market_end(ctx, market)` | A market ends, before settlement |

`ctx` provides: `buy_yes()`, `sell_yes()`, `buy_no()`, `sell_no()`, `cancel_order()`, `cancel_all_orders()`, `position()`, `open_orders`, `books` (all active order books), and `reference_price()` (Binance spot for crypto underlyings).

### Results

```python
result.total_pnl            # net P&L
result.total_return         # as decimal (0.12 = 12%)
result.win_rate             # fraction of profitable settlements
result.sharpe_ratio         # per-settlement Sharpe
result.sortino_ratio        # downside-adjusted
result.max_drawdown         # peak-to-trough as fraction
result.profit_factor        # gross wins / gross losses
result.expectancy           # avg net P&L per settlement

result.trades_df()          # per-fill DataFrame
result.orders_df()          # per-order DataFrame
result.settlements_df()     # per-market settlement P&L
result.equity_df()          # equity curve time series
result.by_series()          # per-series P&L attribution
```

Persist a result to disk and reload it later:

```python
from marketlens.backtest import BacktestResult

result.save("runs/spread-timer")            # or overwrite=True
loaded = BacktestResult.load("runs/spread-timer")
loaded.config, loaded.targets               # config + run inputs preserved
```

The directory holds a JSON manifest plus four Parquet files (`trades`, `orders`, `settlements`, `equity`) — readable directly from pandas/duckdb.

## Data

All list methods return auto-paginating iterators with `.to_list()` and `.to_dataframe()`.

### Order book replay

`walk()` replays full L2 book state for any market or series. Pass a market ID, series slug, or condition ID — the same interface for everything.

```python
walk = client.orderbook.walk(
    "btc-up-or-down-5m",
    after="2026-04-15T01:45:00Z", before="2026-04-15T01:50:00Z",
)
for market, book in walk:
    print(market.question, book.midpoint, book.spread_bps())

# As a DataFrame
df = client.orderbook.walk(
    market_id, after=start, before=end,
).to_dataframe()
```

### Candles, trades, markets

```python
candles = client.markets.candles(
    market_id, resolution="1m",
    after="2026-04-15T01:45:00Z", before="2026-04-15T01:50:00Z",
).to_dataframe()
trades = client.markets.trades(
    market_id,
    after="2026-04-15T01:45:00Z", before="2026-04-15T01:50:00Z",
).to_list()
active = client.markets.list(status="active", sort="-volume", take=10)
```

### Bulk export

Download full history as Parquet — snapshots, deltas, trades, and reference prices.

```python
# Single market (includes reference trades for the underlying)
data_dir = client.exports.download(market_id)

# All markets in a series
data_dir = client.exports.download_series(
    "btc-up-or-down-5m", after="2026-03-01", before="2026-03-08")
```

### Offline backtesting

Download once, run many backtests without API calls:

```python
data_dir = client.exports.download_series(
    "btc-up-or-down-5m", after="2026-03-01", before="2026-03-08")

result = client.backtest(
    strategy, "btc-up-or-down-5m",
    data_dir=data_dir,
    after="2026-03-01", before="2026-03-08",
    initial_cash="10000",
)
```

## Structured Products & Surfaces

For multi-strike series (survival, density, barrier), all sibling markets replay in parallel. `walk.books` holds the latest book for every strike, and `walk.surface()` fits the implied probability distribution at each tick.

```python
walk = client.orderbook.walk(
    "btc-multi-strikes-weekly",
    after="2026-05-08T00:00:00Z",  # picks the next event ending after this
)
for market, book in walk:
    surface = walk.surface()
    if surface:
        for s in surface.survival_strikes():
            print(f"${s.strike:,.0f} P(above)={s.fitted_prob:.3f}")
        print(f"implied_mean=${float(surface.implied_mean):,.0f}")
        break  # the loop fires per book tick — break to print one fit
```

| Type | Source | Stats |
|------|--------|-------|
| `survival` | "above $X" multi-strike markets | `implied_mean`, `implied_cv`, `implied_skew` |
| `density` | Neg-risk range + tail markets | `implied_mean`, `implied_cv`, `implied_skew` |
| `barrier` | Hit-price reach/dip markets | `implied_peak`, `implied_trough` |

Pre-computed surfaces updated every 5 minutes are also available via `client.signals.surfaces()`.

## OrderBook

Every `OrderBook` instance — live or replayed — carries analytical methods:

```python
book.microprice()              # size-weighted mid from best level
book.weighted_midpoint(n=3)    # n-level weighted mid
book.spread_bps()              # spread in basis points
book.imbalance(levels=3)       # bid/ask imbalance [-1, 1]
book.impact("BUY", "1000")     # VWAP for $1k market buy
book.slippage("BUY", "1000")   # slippage from mid
book.depth_within("0.02")      # (bid, ask) depth within 2c of mid
```

## Reference Prices

Binance spot at 1-second resolution for crypto underlyings (BTC, ETH, SOL, XRP, etc.). Available directly or inside backtests via `ctx.reference_price()`.

```python
candles = client.reference.candles(
    "BTC",
    after="2026-04-15T01:45:00Z", before="2026-04-15T01:50:00Z",
    resolution="1s",
)
for candle in candles:
    print(candle.timestamp, candle.close)
```

## API Reference

| Resource | Methods |
|----------|---------|
| `client.markets` | `list()` `get()` `trades()` `candles()` |
| `client.events` | `list()` `get()` `markets()` |
| `client.series` | `list()` `get()` `markets()` `walk()` `events()` |
| `client.orderbook` | `get()` `history()` `metrics()` `walk()` |
| `client.signals` | `surfaces()` `surface()` `history()` |
| `client.reference` | `candles()` `trades()` |
| `client.exports` | `download()` `download_series()` |

Async: use `AsyncMarketLens` — every method has an async counterpart.

## Examples

| Example | Description |
|---------|-------------|
| [`backtest_basic.py`](examples/backtest_basic.py) | Spread-timing strategy on a rolling series |
| [`backtest_limit_orders.py`](examples/backtest_limit_orders.py) | Market-making with CLOB queue position simulation |
| [`backtest_surface.py`](examples/backtest_surface.py) | Surface mispricing with spot-distance filtering |
| [`backtest_portfolio.py`](examples/backtest_portfolio.py) | Multi-series portfolio with shared capital |
| [`execution_cost.py`](examples/execution_cost.py) | Book depth, spread, impact and slippage |
| [`microstructure.py`](examples/microstructure.py) | Feature matrix — does imbalance predict outcome? |
| [`implied_surfaces.py`](examples/implied_surfaces.py) | Survival, density, and barrier surfaces |
| [`event_strikes.py`](examples/event_strikes.py) | Structured product walk with live surface fitting |

## License

MIT
