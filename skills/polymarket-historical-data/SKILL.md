---
name: polymarket-historical-data
description: Get Polymarket historical data in Python with the Marketlens SDK (price history as OHLCV candles, every trade, the full L2 order book at any past moment, tick-level order book history, resolved outcomes, Parquet downloads). Use when the user wants historical or past Polymarket data, price history, odds history, trade history, order book or orderbook snapshots, a dataset for research or machine learning, or needs to download Polymarket data for crypto up or down, weather, sports, esports or election markets.
license: MIT
---

# Polymarket historical data

Polymarket's own APIs serve the current order book and one price series per token; they do not keep past order books. Marketlens records every book update, trade and outcome since 2026-03-01 and serves them through a Python SDK. This skill is the path that works on the first try with a free API key. Read step 2 before pulling anything: a free account has a one time allowance and the tick-level calls can spend it in minutes.

To run a trading strategy over this data, use the `polymarket-backtest` skill from the same repo.

## 1. Setup

```bash
pip install -U marketlens pandas  # Python 3.10+
```

The key is read from the `MARKETLENS_API_KEY` environment variable. If it is not set, ask the user to sign in at https://marketlens.trade/?signin=1&utm_source=skills&utm_medium=agent&utm_campaign=polymarket-historical-data (Google sign in, no card), create a key under Console > API Keys, and export it. Do not write the key into a script or print it. A brand new key can answer 401 for about a minute; wait and retry once before assuming it is wrong.

Check the key and the remaining allowance (this call is free):

```python
import os, httpx
r = httpx.get("https://api.marketlens.trade/v1/series", params={"limit": 1},
              headers={"Authorization": f"Bearer {os.environ['MARKETLENS_API_KEY']}"})
r.raise_for_status()
print(r.headers.get("x-rows-remaining"), "of", r.headers.get("x-rows-limit"), "rows left")
```

## 2. What a free account can do

- **2,000,000 data rows, once.** The allowance does not reset. Each returned candle, trade, book level or order book event bills one row. Catalog calls (`client.markets`, `client.events`, `client.series` list and get) bill nothing.
- **Markets from the last 7 days only.** A market whose data ended earlier raises `HistoryWindowError`. Paid plans read the archive back to 2026-03-01.
- **Tick-level history and Parquet files exist for resolved markets,** usually within 2 hours of resolution. Candles, trades and point-in-time books also work on open markets.

Pick the cheapest call that answers the question. Measured on one BTC 5 minute market (free key, 2026-11-23):

| Call | What it returns | Rows billed |
|---|---|---|
| `client.markets.candles(id, resolution="1m", ...)` | OHLCV from trades | 7 (one per candle) |
| `client.orderbook.metrics(id, resolution="1m", ...)` | best bid and ask, spread, depth per bucket | 7 (one per bucket) |
| `client.orderbook.get(id, at=..., depth=5)` | the L2 book at one instant | 10 (one per level) |
| `client.markets.trades(id, ...)` | every trade | 1,377 (one per trade) |
| `client.exports.download(id, data_dir=...)` | Parquet: every trade, the book exact at each trade | 32,964, then free forever |
| `client.orderbook.walk(id, after=, before=)` over the API | every book change | 19,627 for 30 seconds |
| the same walk with `coalesce=True` | the book at every trade and snapshot | about 6 times fewer |

So: candles and metrics for price and spread history, `trades` for the tape, `exports.download` for tick-level books (then read the file as often as you like), and avoid walking books over the API on a free key.

If a call raises `FreeAllowanceExhaustedError`, `HistoryWindowError` or `IncompleteExportError`, stop and show the user the message: it carries what is needed and the upgrade link (Pro reads the full archive). Do not retry, and do not create another account to get around it; accounts sharing a network get blocked.

## 3. Find the market (free)

```python
from datetime import datetime, timedelta, timezone
from marketlens import MarketLens

client = MarketLens()

# by question text (3 characters or more)
hits = client.markets.list(q="bitcoin", status="active", sort="-volume", take=10, limit=10).to_list()

# by series slug: the most recently resolved markets, newest first
cutoff = datetime.now(timezone.utc) - timedelta(hours=3)
done = client.markets.list(series_id="btc-up-or-down-5m", status="resolved", resolved_before=cutoff,
                           sort="-resolved_at", take=5, limit=5).to_list()

# from a polymarket.com market: its condition id works wherever a market id does
m = client.markets.get("0x...condition id...")
```

`take=` caps the total; without it a list call follows cursors through the whole catalog. Filters on `client.markets.list`: `q`, `event_q`, `status` (`active`, `closed`, `resolved`), `category`, `series_id` (slug or UUID), `event_id`, `subtype`, `min_volume`, `open_after`, `open_before`, `close_after`, `close_before`, `resolved_after`, `resolved_before`, `sort` (`volume`, `liquidity`, `open_time`, `close_time`, `resolved_at`, `created_at`, with `-` for descending).

Every `Market` carries `id`, `question`, `status`, `outcomes` (name and `last_price`), `winning_outcome`, `volume`, `open_time`, `close_time`, `series_id`, `event_id`, `subtype`, and the two fields that matter most here:

- **`data_start` and `data_end`** (epoch ms): the span in which the order book was recorded. `data_end` is `None` while the market is open. Take every `after=` and `before=` from these, never from guessed dates. Both `None` means no book was recorded for that market; skip it.
- **`collection_tier`**: `"polled"` marks a quieter market whose book was recorded as a snapshot every minute or slower instead of every update. Its data is real but coarser.

Series slugs by family:

- Crypto up or down: `btc-up-or-down-5m`, `eth-up-or-down-5m`, `sol-up-or-down-5m`, `xrp-up-or-down-5m`, and the same with `-15m`, `-4h`; `btc-up-or-down-hourly`, `btc-up-or-down-daily`.
- Weather: `<city>-daily-weather` (daily high) and `<city>-daily-lowest-temperature`, for example `nyc-daily-weather`, `london-daily-weather`, `tokyo-daily-weather`. One city and day is one event of about 11 temperature buckets: `client.events.markets(m.event_id).to_list()`.
- Sports and esports: `nba-2026`, `nfl-2026`, `nhl-2026`, `cfb-2026`, `atp`, `wta`, `ufc`, `premier-league-2025`, `counter-strike`, `league-of-legends`, `dota-2`, `valorant`. A league mixes kinds of bet under one slug; filter with `subtype="moneyline"` (or `spread`, `total`). Sports books usually start at the scheduled start (`data_start`), so they cover in-play trading.

There is no text search over series. List them by category: `client.series.list(category="Weather", take=200, limit=200)` (categories include `Crypto`, `Weather`, `Sports`, `Esports`, `Politics`, `Economy`, `Equities`); each has `platform_series_id` (the slug), `title`, `recurrence`, `market_count`.

## 4. Price, spread and trade history

Timestamps accept epoch ms, a `datetime`, or an ISO 8601 string. List calls return iterators with `.to_list()` and `.to_dataframe()`.

```python
m = done[0]

candles = client.markets.candles(m.id, resolution="1m",
                                 after=m.data_start, before=m.data_end).to_dataframe()
# index open_time; columns close_time, open, high, low, close, vwap, volume, trade_count

spread = client.orderbook.metrics(m.id, resolution="1m",
                                  after=m.data_start, before=m.data_end).to_dataframe()
# index t; columns best_bid, best_ask, spread, midpoint, bid_depth, ask_depth, bid_levels, ask_levels

trades = client.markets.trades(m.id, after=m.data_start, before=m.data_end).to_dataframe()
# index platform_timestamp; columns id, price, size, side, fee_rate_bps, ...

print(m.question, "->", m.winning_outcome)
```

Candle resolutions: `1s`, `5s`, `10s`, `30s`, `1m`, `5m`, `15m`, `1h`, `4h`, `1d`. Metrics resolutions: `1m`, `5m`, `15m`, `1h`, `4h`, `1d`. Every price is the price of the first outcome, `m.outcomes[0].name` (Yes, Up, or the first named team), between 0 and 1; the other outcome is 1 minus that. Sizes (trade `size`, candle `volume`, book levels, `bid_depth`, `ask_depth`) are shares, not dollars; a level's dollar value is price times size. A candle exists only for buckets with trades, so quiet markets have gaps; metrics come from the book and have none.

For a market that is still open, pass `before=datetime.now(timezone.utc)`.

Binance spot for the underlying of a crypto market (one row per candle):

```python
spot = client.reference.candles("BTC", resolution="1s",
                                after=m.data_start, before=m.data_end).to_dataframe()
# columns symbol, timestamp, open, high, low, close, volume
```

## 5. The order book at a moment

```python
book = client.orderbook.get(m.id, at=m.data_start + 120_000, depth=10)
print(book.best_bid, book.best_ask, book.spread, book.midpoint)
for level in book.bids[:3]:
    print(level.price, level.size)
print(book.imbalance(levels=3), book.microprice(), book.impact("BUY", 1000))  # average price of a 1,000 share market buy
```

`at` omitted returns the latest book. The answer is the latest book at or before `at` inside the recorded span; check `book.nearest`: `"before"` is the normal case, `"after"` means `at` was earlier than `data_start` and you received the first recorded book instead. An empty side has no real price, so test `book.bid_levels` and `book.ask_levels` before trusting `best_bid`, `best_ask` or `midpoint`.

## 6. Tick-level history as Parquet

Download a resolved market once, then read it locally for free:

```python
import glob
import pandas as pd

path = client.exports.download(m.id, data_dir="data/btc-5m")       # one market
df = pd.read_parquet(glob.glob(f"{path}/history-{m.id}*.parquet")[0])
```

One file per market, `history-<market id>-compact.parquet`, sorted by time, with columns:

| Column | Meaning |
|---|---|
| `event_type` | `snapshot`, `delta` or `trade` |
| `t` | epoch ms |
| `bids`, `asks` | on snapshots: the full book as a JSON string, `[{"price": "0.49", "size": "1007.86"}, ...]` |
| `price`, `size`, `side` | on deltas: the new resting size in shares at `price` on the bid (`BUY`) or ask (`SELL`) side, 0 removes the level. On trades: the fill and its aggressor side |
| `trade_id` | on trades |

A crypto market also gets `reference-BTC-<after>-<before>.parquet` (`timestamp`, `price`: Binance trades for the underlying over that window in epoch ms, one file per window fetched).

The compact file keeps the book exact at every trade and snapshot and drops the churn between trades. Pass `coalesce=False` for every single book change (about 6 times the rows on BTC 5m).

To rebuild books from the file without writing the replay yourself, walk it offline (no rows billed):

```python
for market, book in client.orderbook.walk(m.id, data_dir="data/btc-5m", coalesce=True):
    ...   # book.as_of, book.best_bid, book.best_ask, book.bids, book.asks, book.imbalance()
```

Several markets at once: loop `client.exports.download(x.id, data_dir=...)` over an explicit list of ids. Files you already own download again for free, and `data_dir` passes straight into `client.backtest(..., data_dir=...)`.

A whole series over a window, for series whose markets are all one kind (crypto up or down): quote first, then download.

```python
window = dict(after=done[-1].open_time, before=done[0].close_time)
quote = client.exports.download_series("btc-up-or-down-5m", dry_run=True, **window)
print(len(quote.ready), "markets,", quote.rows_charged, "rows", quote.wall)   # nothing billed yet
data = client.exports.download_series("btc-up-or-down-5m", data_dir="data/btc-5m", **window)
print(len(data.ready), "downloaded,", len(data.pending), "not built yet,", data.rows_charged, "rows")
```

Always pass both `after` and `before`. `quote.wall` is set when the window needs more rows than the account has left; the quote then covers only what fits. On a sports league slug pass `subtype="moneyline"` (or `spread`, `total`), in the quote and in the download: without it the call takes every kind of bet in the window (two days of NBA is about 490 files).

## 7. Errors

| Error | Meaning | Do |
|---|---|---|
| `AuthenticationError` | key missing, wrong, or under a minute old | set `MARKETLENS_API_KEY`, wait a minute, retry once |
| `HistoryWindowError` | the market ended before the free 7 day window | pick a newer market, or show the user the upgrade link in the message |
| `FreeAllowanceExhaustedError`, `IncompleteExportError` | the one time row allowance is spent | stop and tell the user; downloaded files stay usable |
| `ExportNotReadyError` | the market's file is not built (it has not resolved, or resolved under 2 hours ago) | use candles, trades and `orderbook.get`, or a market resolved at least 3 hours ago |
| `DataNotAvailableError` | no book recorded there; carries `data_start`, `data_end` | move the request inside that span |
| `InvalidParameterError` | a bad filter or a range too long for the resolution | read the message; shorten the window or use a coarser resolution |
| `httpx.HTTPStatusError` 504 during a download | the file store timed out | call again with the same `data_dir` |

Everything in `marketlens.exceptions` derives from `MarketLensError`. Full docs: https://marketlens.trade/docs
