# Marketlens

Backtest prediction market strategies on tick-level L2 order book data from Polymarket. Marketlens records every book update, replays it through an execution-realistic engine, and hands you the results as metrics and DataFrames. Write a `Strategy`, point it at a market or series, and know whether it makes money.

[PyPI](https://pypi.org/project/marketlens/) · [Documentation](https://marketlens.trade/docs) · [Changelog](CHANGELOG.md)

```bash
pip install marketlens
```

Python 3.10+. Get a free API key at [marketlens.trade](https://marketlens.trade) and export it as `MARKETLENS_API_KEY`. Order book history starts 2026-03-01.

## Quickstart

```python
from marketlens import MarketLens
from marketlens.backtest import Strategy

class OpeningFader(Strategy):
    def on_market_start(self, ctx, market, book):
        self._entered = False

    def on_book(self, ctx, market, book):
        if self._entered:
            return
        if book.midpoint < 0.50:
            ctx.buy_yes(size=200)
        else:
            ctx.buy_no(size=200)
        self._entered = True

client = MarketLens()  # reads MARKETLENS_API_KEY
result = client.backtest(
    OpeningFader(), "btc-up-or-down-5m",
    initial_cash=10_000,
    after="2026-04-15T01:45:00Z", before="2026-04-15T02:00:00Z",
)
print(result.summary())
```

## Two engines

`client.backtest()` runs one of two engines, chosen by your strategy's base class:

- **Execution** (`Strategy`): replays the full L2 book tick by tick and simulates how your orders actually fill: latency, limit orders, CLOB queue position, fees, settlement. Use it when the edge lives in how you trade.
- **Alpha** (`AlphaStrategy`): one bar per market per `resolution`, built from order book metrics or trade candles. You declare a target exposure and the engine trades the delta to it. Orders, queues, and latency are out of the model, so multi-week and multi-month windows stay fast. Use it to test whether a signal predicts price at all.

A common loop: prove the signal on the alpha engine over a long window, then confirm the execution on the tick engine over a short one.

Docs: [Execution](https://marketlens.trade/docs/backtesting) · [Alpha](https://marketlens.trade/docs/backtesting/alpha) · [Runs](https://marketlens.trade/docs/backtesting/runs) · [Examples](https://marketlens.trade/docs/backtesting/examples)

## Execution backtests

The target is a market UUID, a series slug, or a list of either. Always pass `after`/`before` on series runs, they are otherwise unbounded.

One market, full lifetime by default:

```python
result = client.backtest(strategy, market_id, initial_cash=10_000)
```

A rolling series, every market in `[after, before)`:

```python
result = client.backtest(
    strategy, "btc-up-or-down-5m", initial_cash=10_000,
    after="2026-04-15T01:45:00Z", before="2026-04-15T02:00:00Z",
)
```

A portfolio with shared capital across series:

```python
result = client.backtest(
    strategy, ["btc-up-or-down-5m", "eth-up-or-down-5m", "sol-up-or-down-5m"],
    initial_cash=10_000,
    after="2026-04-15T01:45:00Z", before="2026-04-15T02:00:00Z",
)
```

A structured product, every strike in the matched event replayed together (`ctx.books` holds all of them). Weather series run the same way, every temperature bucket of the day's chain at once:

```python
result = client.backtest(
    strategy, "btc-multi-strikes-weekly", initial_cash=10_000,
    after="2026-05-08T00:00:00Z",  # picks the next event ending after this
)
```

A sports league, one bet type across the day's games:

```python
result = client.backtest(
    strategy, "mlb", subtype="moneyline", initial_cash=10_000,
    after="2026-06-21T17:30:00Z", before="2026-06-22T03:30:00Z",
)
```

Rolling and structured series hold one kind of bet and run whole. A sports league bundles several under one ticker (moneyline, spread, totals, player props), so pass `subtype` to pick one; leave it off and the run stops and lists the choices, so different kinds of bets never mix in one backtest.

The portfolio handles CTF merge automatically: buying NO while holding YES nets matched pairs back to cash at $1 per share.

## Alpha backtests

Subclass `AlphaStrategy`, read the bar, set a target:

```python
from marketlens.backtest import AlphaStrategy

class MomentumTilt(AlphaStrategy):
    def on_market_start(self, ctx, market, bar):
        self._prev = None

    def on_bar(self, ctx, market, bar):
        if self._prev is not None:
            ctx.target_weight(max(-1.0, min(1.0, 50 * (bar.mid - self._prev))))
        self._prev = bar.mid

result = client.backtest(
    MomentumTilt(), "btc-up-or-down-5m",
    initial_cash=10_000,
    resolution="1m", price="mid", fill="next", slippage_bps=5,
    after="2026-03-01T00:00:00Z", before="2026-03-08T00:00:00Z",
)
```

A target is signed YES exposure: `ctx.target_weight(+0.1)` holds YES worth 10% of equity, `-0.1` holds the NO side, `0` goes flat. `ctx.target_position(n)` targets share counts instead. Targets persist until changed, so re-asserting the same target trades nothing.

Alpha results add annualized time-series `sharpe_ratio`, `sortino_ratio`, `volatility`, and `turnover` from the per-bar equity curve. The signal model measures signal quality net of a stylized cost (slippage and fees); anything whose edge lives in the microstructure (queue position, partial fills, latency, spread capture) needs confirming on the tick engine.

## Download once, iterate offline

Pass `data_dir=` to any backtest. Missing files download on the first run and are reused after, so editing a strategy and re-running costs no API events:

```python
result = client.backtest(
    strategy, "btc-up-or-down-5m",
    data_dir="data/btc-5m",
    initial_cash=10_000,
    after="2026-03-01", before="2026-03-08",
)
# tweak the strategy, run again: replays entirely from disk
```

This works for both engines (tick history and alpha bars). To prefetch explicitly, use exports; the result is `os.PathLike` and passes straight into `data_dir=`:

```python
data = client.exports.download_series(
    "btc-up-or-down-5m", after="2026-03-01", before="2026-03-08")
print(data.ready, data.pending, data.failed, data.events_charged)

result = client.backtest(strategy, "btc-up-or-down-5m", data_dir=data,
                         initial_cash=10_000,
                         after="2026-03-01", before="2026-03-08")
```

Exports are Parquet files (snapshots, deltas, trades, and reference prices for the underlying), built server-side. A single market comes via `client.exports.download(market_id)`, which raises `ExportNotReadyError` until its file is built; `download_series` lists such markets under `result.pending` and skips them.

## Results

```python
result.total_pnl            # net P&L
result.total_return         # as decimal (0.12 = 12%)
result.win_rate             # fraction of profitable settlements
result.sharpe_ratio         # per-settlement (annualized time-series in alpha runs)
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

`result.show()` opens an interactive dashboard in the browser. Persist runs and reload or compare them later:

```python
from marketlens.backtest import BacktestResult

result.save("runs/spread-timer")
loaded = BacktestResult.load("runs/spread-timer")     # config + run inputs preserved
BacktestResult.dashboard("runs/a", "runs/b")          # compare saved runs
```

The saved directory holds a JSON manifest plus four Parquet files (`trades`, `orders`, `settlements`, `equity`), readable directly from pandas or duckdb.

Pass a list of strategies to race them over the same window; you get a `MultiBacktestResult` with overlaid equity curves:

```python
result = client.backtest(
    [maker, fader], "btc-up-or-down-5m",
    labels=["maker", "fader"], initial_cash=10_000,
    after="2026-04-15T01:45:00Z", before="2026-04-15T02:00:00Z",
)
result.show()
```

## Market data

Everything the backtester replays is also queryable directly. List methods return auto-paginating iterators with `.to_list()` and `.to_dataframe()`; pass `take=N` to cap total items (iterators otherwise follow cursors to the end).

```python
active = client.markets.list(status="active", sort="-volume", take=10)

candles = client.markets.candles(
    market_id, resolution="1m",
    after="2026-04-15T01:45:00Z", before="2026-04-15T01:50:00Z",
).to_dataframe()

trades = client.markets.trades(
    market_id,
    after="2026-04-15T01:45:00Z", before="2026-04-15T01:50:00Z",
).to_list()

book = client.orderbook.get(market_id, at="2026-04-15T01:45:00Z")  # point-in-time L2
```

To stream reconstructed book states over a window, `client.orderbook.walk()` takes the same targets as the backtester (market ID, series slug, condition ID):

```python
for market, book in client.orderbook.walk(market_id, after=start, before=end):
    print(book.midpoint, book.spread_bps())
```

Every `OrderBook`, live or replayed, carries analytics:

```python
book.microprice()              # size-weighted mid from best level
book.weighted_midpoint(n=3)    # n-level weighted mid
book.spread_bps()              # spread in basis points
book.imbalance(levels=3)       # bid/ask imbalance [-1, 1]
book.impact("BUY", 1000)       # VWAP for $1k market buy
book.slippage("BUY", 1000)     # slippage from mid
book.depth_within(0.02)        # (bid, ask) depth within 2c of mid
```

Binance spot at 1-second resolution is available for crypto underlyings (BTC, ETH, SOL, XRP, etc.), directly or inside backtests via `ctx.reference_price()`:

```python
candles = client.reference.candles(
    "BTC", resolution="1s",
    after="2026-04-15T01:45:00Z", before="2026-04-15T01:50:00Z",
)
```

## Surfaces

Multi-strike series imply a probability distribution over the underlying. Pre-computed surfaces, refreshed every 5 minutes, come from `client.signals.surfaces()`; during a walk over a structured series, `walk.surface()` fits the distribution at the current tick:

```python
walk = client.orderbook.walk("btc-multi-strikes-weekly",
                             after="2026-05-08T00:00:00Z")
for market, book in walk:
    surface = walk.surface()
    if surface:
        print(f"implied_mean=${surface.implied_mean:,.0f}")
        break
```

| Type | Source | Stats |
|------|--------|-------|
| `survival` | "above $X" multi-strike markets | `implied_mean`, `implied_cv`, `implied_skew` |
| `density` | Neg-risk range and tail markets (incl. daily weather) | `implied_mean`, `implied_cv`, `implied_skew` |
| `barrier` | Hit-price reach/dip markets | `implied_peak`, `implied_trough` |

## Agentic access (MCP)

Expose the SDK to any MCP client (Claude Code, Claude Desktop, Cursor) so an agent can research markets, pull book data and surfaces, and author and run backtests in natural language. The server runs locally over stdio with your own API key.

```bash
pip install 'marketlens[mcp]'
```

```json
{
  "mcpServers": {
    "marketlens": {
      "command": "marketlens-mcp",
      "env": { "MARKETLENS_API_KEY": "mk_..." }
    }
  }
}
```

| Tool | Purpose |
|------|---------|
| `search_markets` `get_market` | Find and inspect markets |
| `search_events` `search_series` | Browse events and recurring series |
| `get_orderbook` | Point-in-time L2 book with spread/microprice/imbalance |
| `get_orderbook_metrics` | Time-bucketed book metrics (budget-friendly series) |
| `get_trades` `get_candles` | Executed trades and OHLCV |
| `get_reference_candles` | Binance spot for the underlying |
| `get_signals` `get_surface` | Implied-probability surfaces |
| `strategy_reference` `run_backtest` | Author a `Strategy` and run it through the engine |
| `compare_backtests` `open_backtest` | Score strategies side by side, inspect a saved run |

Tools that bill events (`get_trades`, `get_candles`, `get_orderbook_metrics`, `get_reference_candles`) require both `after` and `before`. `run_backtest` executes agent-authored strategy code in a subprocess on your machine; disable it with `MARKETLENS_MCP_DISABLE_BACKTEST=1`. See the [MCP docs](https://marketlens.trade/docs/mcp).

## Reference

### Strategy hooks

| Hook | Called when |
|------|------------|
| `on_book(ctx, market, book)` | Every book state change (snapshot or delta) |
| `on_trade(ctx, market, book, trade)` | Every executed trade |
| `on_fill(ctx, market, fill)` | Your order is filled |
| `on_reject(ctx, market, order)` | Your order is rejected |
| `on_market_start(ctx, market, book)` | A new market begins |
| `on_market_end(ctx, market)` | A market ends, before settlement |

Execution `ctx` provides `buy_yes()`, `sell_yes()`, `buy_no()`, `sell_no()`, `cancel()`, `cancel_all()`, `position()`, `open_orders`, `cash`, `equity`, `books` (all active order books), and `reference_price()` (Binance spot for crypto underlyings).

`AlphaStrategy` replaces `on_book`/`on_trade` with `on_bar(ctx, market, bar)`, called once per market per bar. Alpha `ctx` provides `target_weight()`, `target_position()`, `bar` (mid, spread, depth, plus OHLCV when `price="close"`), `bars` (every market live this bar), and the same `position()`, `equity`, and `reference_price()`.

### Execution engine parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `latency_ms` | `50` | Order-to-fill delay in milliseconds |
| `queue_position` | `False` | CLOB queue modeling: fills only when queue-ahead is drained by trades |
| `limit_fill_rate` | `0.1` | Fraction of trade size filling your limit (ignored when `queue_position=True`) |
| `slippage_bps` | `0` | Extra price penalty on market order fills |
| `fees` | `"polymarket"` | Auto-detects crypto vs sports fee schedule; `None` for zero fees |
| `max_fill_fraction` | `1.0` | Max fraction of each book level consumed per order |
| `include_trades` | `True` | Fetch trade data (required for limit fills and `on_trade`) |
| `settlement_delay_ms` | `5000` | Delay before filled tokens become sellable (on-chain settlement) |
| `auto_merge` | `True` | Merge matched YES+NO pairs back to cash after each fill (CTF merge) |

### Alpha engine parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `resolution` | `"1m"` | Bar cadence: `1m` to `1d` for `price="mid"`, `1s` to `1d` for `price="close"` |
| `price` | `"mid"` | Bar price: `"mid"` (order book metrics) or `"close"` (trade candles) |
| `fill` | `"next"` | Fill at the next bar's mid (no look-ahead), or `"close"` (same bar) |
| `slippage_bps` | `0` | Price penalty per fill; `5` is a realistic starting point |

The tick-only options (`latency_ms`, `queue_position`, `limit_fill_rate`, `settlement_delay_ms`, `include_trades`) do not apply to alpha runs.

### Numeric conventions

All numeric fields (prices, sizes, volumes, fees, statistics) are `float`, with defaults picked so call sites need no guards: Polymarket prices (`best_bid`, `best_ask`, `midpoint`) default to `0.5` when the side is missing, sizes and rates default to `0.0`. Genuinely optional values (`winning_outcome` before resolution, `strike` on non-structured markets, `book.spread_bps()` on an empty book) return `None`. Detect a truly empty book with `book.bid_levels` / `book.ask_levels`, not by comparing prices to defaults.

### Resources

| Resource | Methods | Docs |
|----------|---------|------|
| `client.markets` | `list()` `get()` `trades()` `candles()` | [Markets](https://marketlens.trade/docs/markets), [Trades & Candles](https://marketlens.trade/docs/trades-candles) |
| `client.events` | `list()` `get()` `markets()` | [Events & Series](https://marketlens.trade/docs/events-series) |
| `client.series` | `list()` `get()` `markets()` `events()` `walk()` | [Events & Series](https://marketlens.trade/docs/events-series) |
| `client.orderbook` | `get()` `history()` `metrics()` `walk()` | [Order Book](https://marketlens.trade/docs/orderbook) |
| `client.signals` | `surfaces()` `surface()` `history()` | [Signals & Surfaces](https://marketlens.trade/docs/signals-surfaces) |
| `client.reference` | `candles()` `trades()` | [Reference Prices](https://marketlens.trade/docs/reference-prices) |
| `client.exports` | `download()` `download_series()` `download_market_bars()` `download_market_bars_batch()` | [Exports](https://marketlens.trade/docs/exports) |

Async: use `AsyncMarketLens`, every method has an async counterpart. See also [Pagination](https://marketlens.trade/docs/pagination) and [Errors & Rate Limits](https://marketlens.trade/docs/errors).

## Examples

| Example | Description |
|---------|-------------|
| [`backtest_basic.py`](examples/backtest_basic.py) | Spread-timing strategy on a rolling series |
| [`backtest_limit_orders.py`](examples/backtest_limit_orders.py) | Market-making with CLOB queue position simulation |
| [`backtest_surface.py`](examples/backtest_surface.py) | Surface mispricing with spot-distance filtering |
| [`backtest_portfolio.py`](examples/backtest_portfolio.py) | Multi-series portfolio with shared capital |
| [`backtest_alpha.py`](examples/backtest_alpha.py) | Signal-level momentum tilt with target weights |
| [`execution_cost.py`](examples/execution_cost.py) | Book depth, spread, impact and slippage |
| [`microstructure.py`](examples/microstructure.py) | Feature matrix: does imbalance predict outcome? |
| [`implied_surfaces.py`](examples/implied_surfaces.py) | Survival, density, and barrier surfaces |
| [`event_strikes.py`](examples/event_strikes.py) | Structured product walk with live surface fitting |

## License

MIT
