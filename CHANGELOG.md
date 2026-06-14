# Changelog

All notable changes to the `marketlens` Python SDK, version by version.

## [1.4.0] 2026-06-14

* New MCP server for agentic access. Install with `pip install 'marketlens[mcp]'` and run the `marketlens-mcp` console script (or `python -m marketlens.mcp`); it serves over stdio and authenticates with `MARKETLENS_API_KEY`. Point any MCP client (Claude Code, Claude Desktop, Cursor) at it to research markets, pull order book data and implied-probability surfaces, and author and run backtests in natural language.
* Data tools wrap the existing typed resources: `search_markets`, `get_market`, `search_events`, `search_series`, `get_orderbook`, `get_orderbook_metrics`, `get_trades`, `get_candles`, `get_reference_candles`, `get_signals`, `get_surface`. List tools return compact rows and cap results at 200 per call; the billed endpoints (`get_trades`, `get_candles`, `get_orderbook_metrics`, `get_reference_candles`) require both `after` and `before`. Known SDK errors (budget exhausted, rate limited, not found) come back as a structured `error` dict the agent can act on instead of a raised exception.
* Strategy authoring tools: `strategy_reference` returns the `Strategy` / `StrategyContext` API so an agent writes correct code, and `run_backtest` executes that code through the backtest engine in a subprocess (hard timeout, progress suppressed) and returns headline metrics plus a saved result path openable later with `.show()`. Code runs locally on the caller's machine; set `MARKETLENS_MCP_DISABLE_BACKTEST=1` to disable it. A failed run returns a structured error; when an export in the window is still building, it includes a `hint` explaining that readiness is progressive and non-contiguous (narrow to a settled window or retry), and the noisy SDK-internal traceback is dropped for expected conditions (kept for genuine strategy-code bugs). Because a backtest downloads and replays data and can take minutes for wide windows, the result now reports `elapsed_s`, `run_backtest` accepts a `data_dir` to cache a window for much faster re-runs, and the tool/instructions recommend validating on a short window first; the timeout message points at the same levers.
* Backtest tooling for portfolios, comparison, and inspection. `run_backtest` now accepts a LIST of targets for a shared-capital multi-asset portfolio (and returns a `by_series` PnL breakdown), and rejects a delimited string with a hint to pass a list. New `compare_backtests` runs several strategies over the same target/window/config and returns their metrics side by side (data downloaded once, so it is cheaper and fair by construction). New `open_backtest` loads a saved result and returns its metrics, per-series PnL, and a per-market settlement ledger, so a run can be inspected without re-running. `strategy_reference` now documents the `Position` schema (`.shares`, not `.size`), the no-shorting / CTF two-sided-quote idiom (`buy_no` at `1 - price`), and asynchronous fills; the `run_backtest` docstring notes that `limit_fill_rate` is ignored when `queue_position=True`.
* Model ergonomics for zero-context use: the server ships `instructions` (id conventions, timestamp formats, the per-row billing rule, and the search/inspect/backtest flow) surfaced at initialize, and enum-valued params (`status`, `order`, `side`, `surface_type`, `resolution`) are declared as JSON-schema enums so valid values are visible up front rather than learned from a rejected call. Caught SDK errors are logged to stderr for the operator while the model still receives the clean error dict. `get_orderbook` reports `two_sided` and returns `null` (not a neutral placeholder) for a side with no resting orders, so a one-sided book never reads as a crossed quote; `get_orderbook` and `get_surface` docstrings spell out the imbalance sign and the implied-stat units.
* The MCP server is an optional extra with no effect on the base install; the `mcp` dependency is only imported when the server runs.

## [1.3.3] 2026-06-11

* `client.backtest(...)` accepts an optional `labels=[...]` (one name per strategy) for multi-strategy runs. The labels name each strategy's `Backtesting` progress bar and become the `MultiBacktestResult` labels; they default to `strategy 1`, `strategy 2`, ….
* Multi-strategy backtests log the resolution-phase status lines (`Resolving N target(s)…`, file-skip notes) once instead of repeating them once per strategy.
* The auto-download progress bar names the series/market being fetched (`Downloading <series>`) instead of a bare `Downloading`.
* Notebook progress bars no longer ghost a new line per tick when a labelled (or otherwise full-width) bar reaches the cell edge: the bar is held one column under the width and its columns crop/ellipsize instead of wrapping onto a second row.

## [1.3.2] 2026-06-07

* Multi-strategy backtests. Pass a list of strategies as the first argument to `client.backtest([s1, s2], ...)` to run each over the same window and get back a `MultiBacktestResult`. Each strategy runs in its own engine (independent portfolio, orders, settlements) so the results are directly comparable.
* `MultiBacktestResult` behaves like a sequence of `BacktestResult` (index by position or label, iterate, `len`), exposes `summary()` and `save(dir)`, and overlays every run in one dashboard via `result.show("name a", "name b", ...)` — names default to the run labels. Reopen saved runs with `MultiBacktestResult.load(dir)` or jump straight to the comparison dashboard with `MultiBacktestResult.dashboard(dir)` (mirrors `BacktestResult.dashboard`).
* New `marketlens.backtest.run_strategies(client, strategies, config, id, ...)` helper backing the list form, exported alongside `MultiBacktestResult`.
* Reference (underlying spot) exports now fetch a 60s lookback before the first market open. A market opening exactly on the window boundary (e.g. midnight) previously had no prior tick — the underlying's first trade lands a few hundred ms later — so `ctx.reference_price()` returned `None` for its opening events. Delete and re-download existing `reference-*.parquet` files to pick up the wider window. The price lookup is unchanged (still the closest tick at/before the query time).
* New `concurrency` parameter on `client.backtest(...)` (default 8, capped to the CPU count) controlling parallel per-market downloads on the auto-download path (`data_dir` set but empty). No effect once the files are already on disk.
* Faster full-firehose replay. Trade-only strategies now reconstruct the order book lazily from deltas, cutting replay time roughly 3x with byte-identical results across single, rolling, multi, and structured backtests.
* Progress bar fixes. Thread-safe bar creation avoids duplicate bars under concurrent prewarm, notebooks render a single clean bar with live PnL/return/win, and skip notes are logged once with correct post-skip totals. Dashboard trade/order timeline markers are now capped so large runs don't crash the browser.

## [1.3.1] 2026-05-27

* **Breaking:** numerical fields (prices, sizes, volumes, fees, OHLCV, strikes, depth) are now `float` instead of decimal strings. `book.best_bid * 0.99` works directly, no `Decimal(...)` wrap. The DB still stores at 4 d.p. precision so float round-trip is lossless within that tick. Callers that compared with `== "0.6500"` need to switch to numeric comparison (`pytest.approx(0.65)` or `abs(x - 0.65) < 1e-4`).
* **Breaking:** fields that the DB guarantees populated are no longer `Optional`. `Market.tick_size`, `Market.created_at`/`updated_at`, `Event.created_at`/`updated_at`, `Trade.price`/`size`/`platform_timestamp`/`collected_at`, `Candle.open`/`high`/`low`/`close`/`volume`/`open_time`/`close_time` drop their `None` branch.
* Fields that are legitimately sometimes-missing fall back to sensible defaults so callers don't need `is None` guards. Polymarket prices (`OrderBook.best_bid`/`best_ask`/`midpoint`, `BookMetrics.best_bid`/`best_ask`/`midpoint`, `Outcome.last_price`) default to `0.5` (the neutral [0, 1] prior). Sizes (`spread`, `bid_depth`/`ask_depth`, `Candle.vwap`, `Trade.fee_rate_bps`, `Market.volume`/`liquidity`) default to `0.0`. Detect a truly empty book with `book.bid_levels` / `book.ask_levels`. Genuinely optional fields with semantic `None` (unresolved markets' `winning_outcome`, structured-only `strike`) stay `Optional`.
* `BacktestResult` saved files now use `format_version=2` (float-typed parquet + JSON). Older `format_version=1` saves cannot be loaded; rerun the backtest to regenerate.
* Backtest engine internals (`Portfolio`, `FillSimulator`, `_results.BacktestResult`) operate on `float` throughout. `Fill`/`Order`/`Position`/`SettlementRecord` expose numeric fields as `float`. `initial_cash` still accepts `float | int | str` for backwards compatibility.
* Interactive backtest dashboard. Call `result.show()` to open a local browser dashboard with equity curve, drawdown, PnL by market, PnL distribution, trade timeline, order analysis, and a sortable settlements table. Zero new dependencies — uses plotly.js via CDN and Python's stdlib HTTP server.
* Multi-run comparison. Pass additional results to `result.show(other)` or load from disk with `BacktestResult.dashboard("path1", "path2")`. Charts overlay runs, metrics highlight the best value per row, and toggle checkboxes control visibility.
* Market names stored in backtest results. The engine now persists `Market.question` text alongside market IDs so the dashboard, charts, and tables display human-readable names instead of UUIDs. Backward compatible — older saved results fall back to truncated IDs.
* Offline backtests on structured series (multi-strike products, e.g. `btc-multi-strikes-weekly`) over a window narrower than the market lifetime now skip pre-window parquet rows instead of replaying the whole file. A 1h backtest against weekly markets runs roughly 14x faster end-to-end on a cached `data_dir`.
* Streaming backtests on structured series fan out per-lane prefetchers concurrently. Time-to-first-event drops from N round-trips to a small parallel batch for products with many overlapping markets. Rolling series and single-market backtests are unaffected (they run on a single lane).
* Streaming path no longer surfaces the API's anchor snapshot (delivered at `t <= after` to seed the order book) to the strategy. Matches the offline path's `[after, before)` half-open contract exactly.

## [1.3.0] 2026-05-26

* New `DailyBudgetExceededError` exception for 429 responses with error code `DAILY_BUDGET_EXCEEDED`. Raised when the caller's daily event budget is exhausted (resets at midnight UTC). Unlike `RateLimitError`, this is NOT auto-retried by the SDK since the budget won't reset for hours.
* RPM-based `RateLimitError` (429, code `RATE_LIMITED`) continues to be auto-retried with exponential backoff as before.
* Rate limit and budget exhaustion error messages now include upgrade information for free-tier users.
* New `on_reject(ctx, market, order)` strategy hook fires when the engine rejects an order (empty book, insufficient cash at activation, duplicate sell from latency). Distinct from user initiated cancellations so strategies can react to adverse fills.
* Orders now fill against the live book at activation time, not the book at submission. An order in flight during the latency window sees price drift and depth changes, modeling real world adverse selection.

## [1.2.2] 2026-05-18

* Series export responses now include a `rate_limited` list of markets skipped because including them would exceed the caller's daily event budget. A new `SeriesRateLimited` dataclass carries the market id and event count; retry after budget reset or with a narrower `after`/`before` window.

## [1.2.1] 2026-05-17

* Backtests fetch missing market data automatically: if `data_dir` is empty or absent, required exports download on first run with no explicit `client.exports.download()` call needed.
* More accurate progress bar updates during concurrent downloads.
* Tighter price time priority handling in limit order fill simulation.

## [1.2.0] 2026-05-15

* Pre built market exports with explicit status. `download_series()` now returns a `SeriesDownloadResult` listing `ready` markets, `pending` exports with their build status, and `failed` markets. Presigned download URLs allow parallel fetching through a thread pool.
* New `ExportNotReadyError` exception for markets whose pre built export is not yet available.
* Unified API across single market and series exports, with transparent handling of bulk computed and streaming sources.

## [1.1.2] 2026-05-10

* Consistent timestamp parsing across `events`, `signals`, `markets`, `series`, and `orderbook.history`: every endpoint accepts both epoch milliseconds and `datetime` objects.
* Streaming and bulk export paths now read identical per market event windows, clamped to `[open_time, close_time)` intersected with the user supplied range.
* Clearer error messages when event timestamps fail to parse.

## [1.1.1] 2026-05-06

* Streaming bulk downloads for series exports. Large series archives stream straight to disk instead of buffering the full response in memory, dropping peak memory use on big backtests.
* New `stream_bytes()` HTTP client method, integrating retry and progress reporting with streaming parsers.

## [1.1.0] 2026-05-04

* Automatic compact data path. The engine introspects the strategy's `on_book` hook to decide whether trades alone are sufficient, then downloads the space efficient compact parquet variant (trade snapshots only) instead of the full firehose.
* New `coalesce` parameter (None for auto, True to force compact, False to force full) for explicit control of the data path.
* Per market variant selection: the engine picks the available file that matches the strategy's needs, with a clear stderr note when it falls back.

## [1.0.10] 2026-05-03

* Persistent backtest results. `BacktestResult.save()` writes trades, orders, settlements, and the equity curve as parquet files plus a JSON manifest; `BacktestResult.load()` reconstructs the full result with metrics and dataframes intact.
* New `take` parameter on order submission to cap position growth (relative or absolute).
* Iterator based pagination on the markets endpoint.

## [1.0.9] 2026-05-03

* ISO 8601 string parsing on every timestamp field across `events`, `signals`, and market data, alongside the existing epoch milliseconds.
* Corrected candle timestamp handling in backtests so reference price lookups are no longer off by one second.

## [1.0.8] 2026-05-02

* Rich based progress reporter for backtests and downloads. Reports current status, per byte progress, and a gzip aware total size estimate.
* Lower latency backtest event stream with prefetching and lane packed structured events.
* Cross market lookahead detection and lazy reference price loading for multi market backtests.
* Native parquet reader path for faster sequential reads than PyArrow.

## [1.0.7] 2026-04-14

* Unified download API. `client.exports.download()` and `download_series()` now share a single result type carrying `ready`, `pending`, `failed`, and `events_charged`.
* Better error reporting for incomplete exports.

## [1.0.6] 2026-04-13

* CTF token pair netting at settlement. Buying the opposite side (e.g. NO while holding YES) now automatically nets matched YES plus NO pairs and credits `matched * $1` back to cash, mirroring complementary token fusion on the exchange.
* Crossing limit orders fill correctly. A limit at a price across the spread takes resting liquidity up to the limit price, then rests any remainder as a maker order.
* Crossed books (bid greater than or equal to ask) accepted under mid market conditions.
* Subsecond granularity on reference price trades for crypto underlyings.

## [1.0.5] 2026-03-24

* Warning surfaced when a backtest has no market data, preventing silent empty results.

## [1.0.4] 2026-03-24

* Lookahead bias removed from reference prices. Backtests now use the previous second's candle close instead of the current second, so prices reflect only information a live system would have at that moment.

## [1.0.3] 2026-03-23

* New `settlement_delay_ms` parameter (default 5000) controls when matched positions become available for offsetting sells, modeling on chain confirmation latency.
* Requires PyArrow at install time for parquet handling.

## [1.0.2] 2026-03-18

* Optional `data_dir` parameter on backtests reads pre downloaded market history from local parquet files instead of hitting the API, making iterative backtesting fast.
* Cleaner exports orchestration with parallel file handling.

## [1.0.1] 2026-03-17

* Fixes around missing reference prices and market ids in the strategy context.
* General robustness improvements in the backtest engine.

## [1.0.0] 2026-03-09

* First stable release. Backtest engine, exports, and public API surface stabilized.

## [0.5.0] 2026-03-09

* Bulk exports SDK. New `client.exports` resource downloads market history and reference trades as parquet files for offline analysis and backtesting.
* PnL over time tracking on backtest results.
* Engine performance improvements throughout.
* Internal refactor adding signals support.
* Barrier payouts and implied probability surfaces for exotic derivatives.

## [0.4.0] 2026-03-07

* Backtest engine. Tick by tick replay of real order book history with configurable latency, slippage, fees (Polymarket, flat, or zero), and queue position estimation.
* Strategy framework with `on_book()` and related hooks for event driven logic.
* `BacktestConfig` for fine grained simulation parameters.
* New `signals` resource for market derived indicators.

## [0.3.0] 2026-03-05

* `OrderBookWalk` refactored to iterate `(Market, OrderBook)` tuples across a series or single market, with optional `after`/`before` filtering and `to_dataframe()` support.
* Removed `MarketSlot` and `AsyncMarketSlot` in favor of the simpler walk API.
* Series resolution accepts both API ids and platform slugs.

## [0.2.0] 2026-03-05

* Renamed `MarketSlot` and `AsyncMarketSlot` to `OrderBookWalk` and `AsyncOrderBookWalk` for clarity.
* Orderbook first API redesign. `Orderbook.walk()` becomes the primary interface for cross market analysis.
* New orderbook metrics: `microprice()` (size weighted midpoint) and `spread_bps()` (spread in basis points).
* `imbalance()` accepts a configurable level depth for top of book analysis.
* Helpers refactored to share book metrics and dataframe conversion logic.
* Examples updated to the new API.

## [0.1.0] 2026-03-02

* Initial SDK release. Sync and async `MarketLens` client.
* Markets, series, and events list endpoints with automatic pagination.
* Order book snapshots and history replay via `OrderBookReplay` and `AsyncOrderBookReplay`.
* Walk interface for iterating markets in a series with lazy data loaders for candles, trades, and order books.
* Helpers for data format conversion and book reconstruction from deltas.
* Test suite and examples covering backtesting, microstructure analysis, and series replay.
