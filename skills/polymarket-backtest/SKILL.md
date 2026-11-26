---
name: polymarket-backtest
description: Backtest a Polymarket trading strategy against real historical order books with the Marketlens Python SDK (tick-level L2 replay with fees, latency, limit order queue and settlement at the real outcome). Use when the user wants to backtest, simulate, replay or evaluate a Polymarket or prediction market strategy or trading bot, on crypto up or down markets (BTC, ETH, SOL 5m, 15m, hourly), daily weather temperature markets, sports and esports games, elections or any other Polymarket market.
license: MIT
---

# Polymarket backtest

Marketlens records every Polymarket order book update and replays it through a backtest engine: you write a `Strategy` class, point it at markets, and get P&L settled at the real outcome. This skill is the path that works on the first try with a free API key. Follow the steps in order; step 2 matters most, because a free account has a one time allowance and one careless call can spend all of it.

For pulling raw data (candles, trades, books, Parquet files) without a strategy, use the `polymarket-historical-data` skill from the same repo.

## 1. Setup

```bash
pip install -U marketlens     # Python 3.10+, needs 1.8.13 or later
```

The key is read from the `MARKETLENS_API_KEY` environment variable. If it is not set, ask the user to sign in at https://marketlens.trade/?signin=1&utm_source=skills&utm_medium=agent&utm_campaign=polymarket-backtest (Google sign in, no card), create a key under Console > API Keys, and export it. Do not write the key into a script or print it. A brand new key can answer 401 for about a minute; wait and retry once before assuming it is wrong.

Check the key and the remaining allowance (this call is free):

```python
import os, httpx
r = httpx.get("https://api.marketlens.trade/v1/series", params={"limit": 1},
              headers={"Authorization": f"Bearer {os.environ['MARKETLENS_API_KEY']}"})
r.raise_for_status()
print(r.headers.get("x-rows-remaining"), "of", r.headers.get("x-rows-limit"), "rows left")
```

## 2. What a free account can do

- **2,000,000 data rows, once.** The allowance does not reset. Every order book event, trade or bar the server sends bills one row. Catalog calls (`client.markets`, `client.events`, `client.series` list and get) bill nothing, so explore freely.
- **Markets from the last 7 days only.** A market whose data ended earlier raises `HistoryWindowError`. Order book history starts 2026-03-01 on paid plans.
- **Only resolved markets replay.** History files are built after a market resolves, usually within 2 hours. An open market is skipped with the reason `market not resolved yet`. Pick markets that resolved at least 3 hours ago.

Measured row costs (free keys, 2026-11-23). Use them to size a run before you start it:

| What | Rows |
|---|---|
| One BTC 5m market, strategy uses `on_trade` | about 28,000 |
| The same market, strategy overrides `on_book` | about 164,000 |
| One NBA moneyline game | 60,000 to 90,000 |
| One ATP match, one Premier League leg | 5,000 to 10,000 |
| One Counter-Strike match winner | about 34,000 |
| One NYC weather day, all 11 temperature buckets, whole life | about 85,000 |
| Alpha engine (`AlphaStrategy`), streamed, any market | 1 per bar per market |
| Binance reference ticks that `data_dir` adds on crypto markets (`Strategy` runs) | about 1,000 per minute of window |

Four rules follow from that table:

1. **Write `on_trade`, not `on_book`,** unless the strategy truly needs every book change between trades. A strategy that does not override `on_book` gets the compact history (the book is still exact at every trade), about 6 times fewer rows.
2. **Pass `data_dir=` on every `Strategy` run.** Files download once; every rerun over the same markets is free and takes about a second. Without it each run bills again. (The alpha engine in step 7 is cheap enough to stream.)
3. **Start with 1 to 3 markets** (for weather, the buckets of one day). Check that orders fill and the numbers make sense, then widen, multiplying the per market cost by the market count first (busy markets cost two or three times a quiet one, so size from the heaviest market in your sample). The allowance is the user's and does not come back: keep one widening step under about a quarter of the rows left, say what it will cost before running it, and ask before going further.
4. **On a league slug always pass `subtype`** (step 5). With it a window downloads and bills only that kind of bet: one NBA game is 1 moneyline file of 62,571 rows, against 65 files and 129,375 rows for every kind of bet in the same window.

**Two engines, pick by sample size.** The execution engine (`Strategy`, steps 4 to 6) replays every trade against the real book and is the one to trust for fills, but a free allowance covers about 70 BTC 5m markets of it. When the question needs hundreds of markets ("over the past week"), use the alpha engine (`AlphaStrategy`, step 7): it runs on 1 minute bars at one row each, applies the same fees and settles at the real outcome, so a full day of ETH 15m markets (96 of them) measured 1,423 rows and 2 seconds. Prove the idea there, then confirm fills on a handful of markets with the execution engine. Do not compute P&L by hand from bars: fees and settlement are easy to get wrong and the engine already has them.

If a call raises `FreeAllowanceExhaustedError`, `HistoryWindowError` or `IncompleteExportError`, stop and show the user the message: it carries what is needed and the upgrade link (Pro reads the full archive). Do not retry, and do not create another account to get around it; accounts sharing a network get blocked.

## 3. Find the markets (free)

Every `Market` carries `open_time`, `close_time`, `data_start`, `data_end` (epoch ms), `status`, `winning_outcome`, `subtype` and `series_id`. `data_start` to `data_end` is the span that actually replays; `data_end` is `None` while the market is open. Choose windows from these fields, never from guessed dates.

```python
from datetime import datetime, timedelta, timezone
from marketlens import MarketLens

client = MarketLens()
cutoff = datetime.now(timezone.utc) - timedelta(hours=3)   # history files exist by then

def recent(series, n, **filters):
    """The n most recently resolved markets of a series, newest first."""
    found = client.markets.list(series_id=series, status="resolved", resolved_before=cutoff,
                                sort="-resolved_at", take=n, limit=n, **filters).to_list()
    return [m for m in found if m.data_start]      # None means no book was recorded
```

`take=` caps the total; without it a list call follows cursors through the whole catalog.

Series slugs by family:

- Crypto up or down: `btc-up-or-down-5m`, `eth-up-or-down-5m`, `sol-up-or-down-5m`, `xrp-up-or-down-5m`, and the same with `-15m`, `-4h`; `btc-up-or-down-hourly`, `btc-up-or-down-daily`.
- Weather: `<city>-daily-weather` (daily high) and `<city>-daily-lowest-temperature`, for example `nyc-daily-weather`, `london-daily-weather`, `tokyo-daily-weather`.
- Sports and esports: `nba-2026`, `nfl-2026`, `nhl-2026`, `cfb-2026`, `atp`, `wta`, `ufc`, `premier-league-2025`, `counter-strike`, `league-of-legends`, `dota-2`, `valorant`.

Search series by title or slug: `client.series.list(q="tokyo", take=20, limit=20)`. Or list them by category: `client.series.list(category="Weather", take=200, limit=200)` (categories include `Crypto`, `Weather`, `Sports`, `Esports`, `Politics`, `Economy`, `Equities`); each has `platform_series_id` (the slug), `title`, `recurrence`. For anything without a series (elections, one-off events), search markets by question: `client.markets.list(q="fed rate cut", sort="-volume", take=10, limit=10)`.

Look before you trade. A few bars show how a market moved and cost almost nothing:

```python
m = recent("btc-up-or-down-5m", 1)[0]
bars = client.orderbook.metrics(m.id, resolution="1m", after=m.data_start, before=m.data_end).to_dataframe()
print(m.question, "->", m.winning_outcome)
print(bars[["best_bid", "best_ask", "spread", "bid_depth", "ask_depth"]])
```

## 4. Write the strategy

```python
from marketlens.backtest import Strategy

class FadeExtremes(Strategy):
    """Once per market, buy the cheap side when the mid leaves 0.35 to 0.65."""

    def __init__(self):
        self.done = set()

    def on_trade(self, ctx, market, book, trade):
        if market.id in self.done or not (book.bid_levels and book.ask_levels):
            return
        if book.midpoint < 0.35:
            ctx.buy_yes(size=100)
        elif book.midpoint > 0.65:
            ctx.buy_no(size=100)
        else:
            return
        self.done.add(market.id)
```

Hooks, override any subset:

| Hook | Called when |
|---|---|
| `on_market_start(ctx, market, book)` | a market's replay begins |
| `on_trade(ctx, market, book, trade)` | every real trade; `trade.price`, `trade.size`, `trade.side`, `trade.t` |
| `on_book(ctx, market, book)` | every book change (full history, about 6 times the rows) |
| `on_fill(ctx, market, fill)` | your order fills; `fill.side` (`"BUY_YES"`, `"SELL_YES"`, `"BUY_NO"` or `"SELL_NO"`), `fill.size`, `fill.price`, `fill.fee`, `fill.is_maker` |
| `on_reject(ctx, market, order)` | your order is rejected |
| `on_market_end(ctx, market)` | the market's data ends, before settlement |

`ctx`:

- `ctx.buy_yes(size, *, limit_price=None, market_id=None, cancel_after=None)`, and the same signature for `buy_no`, `sell_yes`, `sell_no`. `size` is shares. No `limit_price` means a market order that walks the real book; with one, the order rests and fills as real trades cross it. `cancel_after` is an epoch ms time, for example `ctx.time + 30_000`.
- `ctx.cancel(order)`, `ctx.cancel_all()`.
- `ctx.position()` returns `side` ("YES", "NO" or "FLAT"), `shares`, `avg_entry_price`, `unrealized_pnl`, `realized_pnl`. The field is `shares`, not `size`.
- `ctx.cash`, `ctx.equity`, `ctx.open_orders`, `ctx.time` (epoch ms), `ctx.books` (dict of market id to book for every market live right now).
- `ctx.reference_price()` is the Binance spot price of the underlying on crypto markets, `None` elsewhere.

`book`: `midpoint`, `best_bid`, `best_ask`, `spread`, `bid_levels`, `ask_levels`, `bids` and `asks` (lists with `.price`, `.size`), plus `microprice()`, `imbalance(levels=3)`, `spread_bps()`, `impact("BUY", shares)`. Prices are floats between 0 and 1; sizes are shares.

Things that trip people up:

- Several markets can be live at once (every weather bucket, every game of an evening). Keep state per market, keyed by `market.id` as above; state reset in `on_market_start` is wiped whenever any market starts.
- An empty side has no real price. Check `book.bid_levels and book.ask_levels` before trusting `best_bid`, `best_ask` or `midpoint`. Weather buckets far from the forecast often have one empty side for hours, so compare the buckets that do have two sides instead of waiting for all of them.
- Fills arrive `latency_ms` later (default 50), so `ctx.position()` is unchanged inside the hook that placed the order. React in `on_fill`, or track what you sent.
- YES is the market's first outcome, `market.outcomes[0].name` (Yes, Up, or the first named team), and every book price is its price. NO is the other outcome at 1 minus that. Print the name before reasoning about which team or direction a position holds.
- Positions are long only. `sell_yes` reduces a YES position; to bet against YES, `buy_no`. Holding YES and NO together merges back to $1 cash per pair.
- Whatever is still held at the end settles at $1 or $0 on the real outcome.
- Sports markets usually have `data_start` at the scheduled start, so a game replays in-play trading.
- Some quieter markets are recorded as a book snapshot every minute or slower rather than every update (`market.collection_tier == "polled"`). They replay, and cost few rows, but fills are coarser; say so when reporting.

## 5. Run short, then widen

`client.backtest(strategy, target, *, initial_cash, ...)`. `initial_cash` is required. The target is a market id, a list of market ids, or a series slug with `after=` and `before=` (a datetime, epoch ms or an ISO 8601 string).

**Crypto up or down** (one market live at a time, so the slug and a window work):

```python
done = recent("btc-up-or-down-5m", 2)
result = client.backtest(
    FadeExtremes(), "btc-up-or-down-5m",
    after=done[-1].open_time, before=done[0].close_time,
    initial_cash=10_000, data_dir="data/btc-5m",
)
print(result.summary())
```

To widen, raise the count: `recent("btc-up-or-down-5m", 24)` is two hours and about 800,000 rows with an `on_trade` strategy.

**Weather** (every temperature bucket of one city and day is one event; replay them together):

```python
m = recent("nyc-daily-weather", 1)[0]
ids = [x.id for x in client.events.markets(m.event_id)]
result = client.backtest(FadeExtremes(), ids, initial_cash=10_000, data_dir="data/nyc-weather")
```

`ctx.books` then holds all the buckets, and `market.question` names each one. A week of one city (77 markets) takes about 4 minutes to replay. To cover several days, pass the city slug with `after=` and `before=`: every day's buckets that trade inside the window replay (most weather volume trades after 12:00 UTC on the target day).

**Sports and esports** (a league mixes several kinds of bet under one slug, so choose one with `subtype`: `moneyline`, `spread` or `total`):

```python
games = recent("nba-2026", 2, subtype="moneyline")
result = client.backtest(FadeExtremes(), [g.id for g in games],
                         initial_cash=10_000, data_dir="data/nba")
```

To widen to every game in a window, pass the league slug: `client.backtest(FadeExtremes(), "nba-2026", subtype="moneyline", after=..., before=..., initial_cash=10_000, data_dir="data/nba")`.

Useful options, all keyword: `fees="polymarket"` (default: taker fees on Crypto and Sports category markets, zero on every other category, so mention that when reporting a weather or esports result; `None` for zero everywhere), `latency_ms=50`, `slippage_bps=0`, `limit_fill_rate=0.1` (the share of each crossing trade a resting limit order gets), `queue_position=False` (set `True` to fill a limit only after the size queued ahead of it trades; needs the full history), `progress=False` to silence the progress bars.

A run that streams (no `data_dir`) and ends mid-way on the allowance raises `FreeAllowanceExhaustedError`; with `data_dir` the files that fit are kept and `IncompleteExportError` reports `rows_needed`.

## 6. Read the result

```python
print(result.summary())            # total_pnl, total_return, win_rate, max_drawdown, total_trades, ...
print(result.settlements_df())     # one row per market held to resolution, with pnl and winning_outcome
print(result.trades_df())          # every fill
for s in result.skipped:           # markets that did not replay, and why
    print(s.reason, s.question)
result.save("runs/fade-v1", overwrite=True)   # reload with BacktestResult.load("runs/fade-v1")
```

Also `result.total_pnl`, `total_return`, `win_rate`, `sharpe_ratio`, `max_drawdown`, `profit_factor`, `equity_df()`, `orders_df()`, `by_series()`, and `result.show()` for a browser dashboard.

Before reporting numbers to the user, check three things: `total_trades` is above zero (if not, the entry condition never fired or orders were rejected for cash), `result.skipped` is empty or explained, and the sample is large enough to mean something. Two markets prove the code runs; they say nothing about an edge. Tell the user how many markets the result covers and what widening would cost.

`win_rate` counts positions held to resolution. A strategy that exits before the end can show 0 there with a positive `total_pnl`; judge it on `total_pnl` and `total_return`.

## 7. Many markets: the alpha engine

Subclass `AlphaStrategy`. It sees one bar per market per `resolution` and sets a target position; the engine trades to it at the next bar, charges fees and settles at the real outcome.

```python
from marketlens.backtest import AlphaStrategy

class LeaderAtTen(AlphaStrategy):
    """At minute 10 of a 15 minute market, hold 100 shares of the side that leads."""

    def __init__(self):
        self.done = set()

    def on_bar(self, ctx, market, bar):
        if market.id in self.done or bar.t - market.open_time < 10 * 60_000:
            return
        ctx.target_position(100 if bar.mid > 0.5 else -100)
        self.done.add(market.id)

end = recent("eth-up-or-down-15m", 1)[0].close_time
result = client.backtest(
    LeaderAtTen(), "eth-up-or-down-15m",
    after=end - 24 * 3600 * 1000, before=end,
    initial_cash=10_000, resolution="1m", slippage_bps=5,
)
print(result.summary())
```

- `ctx.target_position(n)` holds `n` YES shares (negative holds NO, 0 is flat); `ctx.target_weight(w)` holds YES worth `w` of equity. A target persists until changed.
- `bar` has `t` (epoch ms), `mid`, `spread`, `bid_depth`, `ask_depth`. `resolution` runs from `1m` to `1d`.
- Fills happen at the next bar's mid, so there is no look ahead, but also no spread: `slippage_bps=5` is a reasonable stand-in. Orders, queues and latency are not modeled.
- Run it without `data_dir`: a streamed alpha run bills one row per bar and is cheap enough to repeat. `ctx.reference_price()` adds one row per minute of window, and only if the strategy calls it.
- The target takes the same forms as in step 5. A crypto slug with `after=` and `before=` is the usual one; a sports league slug needs `subtype=`; a weather city slug works the same way as a crypto one.

## 8. Errors

| Error | Meaning | Do |
|---|---|---|
| `AuthenticationError` | key missing, wrong, or under a minute old | set `MARKETLENS_API_KEY`, wait a minute, retry once |
| `HistoryWindowError` | the market ended before the free 7 day window | pick a newer market, or show the user the upgrade link in the message |
| `FreeAllowanceExhaustedError`, `IncompleteExportError` | the one time row allowance is spent | stop and tell the user; files already in `data_dir` still replay free |
| `ValueError: ... has multiple subtypes` | a sports slug without `subtype` | pass `subtype="moneyline"` (or `spread`, `total`) |
| skipped: `market not resolved yet` or `export pending` | no history file yet | choose markets resolved at least 3 hours ago |
| `total_trades` is 0 | nothing fired or filled | print the bars from step 3 and check the entry condition against them |
| `httpx.HTTPStatusError` 504 during a download | the file store timed out | rerun with the same `data_dir`; finished files are kept |

Everything in `marketlens.exceptions` derives from `MarketLensError`. Full docs: https://marketlens.trade/docs/backtesting
