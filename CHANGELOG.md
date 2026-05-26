# Changelog

All notable changes to the `marketlens` Python SDK, version by version.

## [1.3.0] 2026-05-26

* New `DailyBudgetExceededError` exception for 429 responses with error code `DAILY_BUDGET_EXCEEDED`. Raised when the caller's daily event budget is exhausted (resets at midnight UTC). Unlike `RateLimitError`, this is NOT auto-retried by the SDK since the budget won't reset for hours.
* RPM-based `RateLimitError` (429, code `RATE_LIMITED`) continues to be auto-retried with exponential backoff as before.
* Rate limit and budget exhaustion error messages now include upgrade information for free-tier users.

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
