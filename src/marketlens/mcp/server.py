"""marketlens MCP server.

Exposes the SDK's data products and backtest engine as MCP tools so an agent
(Claude Code, Claude Desktop, Cursor, ...) can research markets, pull order
book data and implied-probability surfaces, and author + run backtests in
natural language. Runs locally over stdio with the user's own API key.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Literal

from marketlens import MarketLens
from marketlens.exceptions import MarketLensError

from marketlens.mcp import _format as fmt

_log = logging.getLogger("marketlens.mcp")

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError(
        "The MCP server needs the 'mcp' package. Install it with:\n"
        "    pip install 'marketlens[mcp]'"
    ) from exc


# Total-item cap shared by the list tools; keeps any single call cheap.
_MAX_LIMIT = 200
_DEFAULT_ARTIFACTS = "./marketlens-backtests"
# A backtest slower than this (seconds) gets a "this was slow, iterate smaller"
# note in its result. Time-based, so it is independent of market type/duration.
_SLOW_RUN_S = 300

# Enum-valued params, surfaced as JSON-schema `enum`s so the model sees the
# valid values up front instead of learning them from a rejected call.
_Status = Literal["active", "closed", "resolved"]
_Order = Literal["asc", "desc"]
_Side = Literal["BUY", "SELL"]
_SurfaceType = Literal["survival", "density", "barrier"]
_CandleRes = Literal["1s", "5s", "10s", "30s", "1m", "5m", "15m", "1h", "4h", "1d"]
_MetricRes = Literal["1m", "5m", "15m", "1h", "4h", "1d"]

_INSTRUCTIONS = """\
marketlens gives you Polymarket prediction-market data and a backtest engine.

IDs: search_markets / search_series / search_events return ids. get_market,
get_orderbook, get_orderbook_metrics, get_trades and get_candles take a market
UUID; run_backtest, compare_backtests and search_series also accept a series
slug like "btc-up-or-down-5m".

Timestamps (after / before / at): epoch ms or an ISO 8601 string like
"2026-03-01T12:00:00Z".

Coverage: history runs from 2026-03-01 up to roughly 3 hours ago; the most
recent few hours are not built yet, so keep windows within that range.

Budget: get_trades, get_candles, get_orderbook_metrics and get_reference_candles
bill one event per returned row, so pass a tight after/before window and a small
limit. Errors come back as {"error", "message"} (plus "hint" when useful); read
the message and adjust rather than retrying unchanged.

Work iteratively and data-first: explore with the data tools to form a
hypothesis, then read strategy_reference and validate the strategy on a SHORT
window before widening deliberately: runtime grows with the data in the window,
so prefer several quick passes to one long backtest. Reuse a data_dir to make
re-runs over the same window fast, compare_backtests to score variants over one
window, and open_backtest for a saved run's per-market detail."""

_client: MarketLens | None = None


def _get_client() -> MarketLens:
    """Lazily build a single client from the environment.

    Reads ``MARKETLENS_API_KEY`` (required) and an optional
    ``MARKETLENS_BASE_URL`` override.
    """
    global _client
    if _client is None:
        base_url = os.environ.get("MARKETLENS_BASE_URL")
        kwargs: dict[str, Any] = {}
        if base_url:
            kwargs["base_url"] = base_url
        _client = MarketLens(**kwargs)
    return _client


def _safe(fn: Callable) -> Callable:
    """Return known SDK errors as a clean dict instead of raising.

    Budget/rate-limit/not-found errors become an actionable signal the agent
    can read and adjust to, rather than an opaque tool failure.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except MarketLensError as exc:
            # Log for the operator watching stderr; hand the model a clean dict.
            _log.info("%s -> %s: %s", fn.__name__, type(exc).__name__, exc)
            return {"error": type(exc).__name__, "message": str(exc)}

    return wrapper


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), _MAX_LIMIT))


def _invoke_runner(cfg: dict, timeout_s: int, strategy_file: str) -> dict:
    """Run the backtest subprocess, timing it and parsing its JSON result.

    Adds `elapsed_s` and the authored `strategy_file` path to every result, plus
    a long-run note when the wall-clock is large.
    """
    env = {**os.environ, "MARKETLENS_PROGRESS": "0"}
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "marketlens.mcp._runner"],
            input=json.dumps(cfg),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "error": "Timeout",
            "message": (
                f"Backtest exceeded {timeout_s}s. Runtime scales with window "
                f"width and market count: validate on a short window first, "
                f"reuse a data_dir to skip re-downloading, or raise timeout_s."
            ),
            "elapsed_s": round(time.monotonic() - started, 1),
            "strategy_file": strategy_file,
        }
    elapsed_s = round(time.monotonic() - started, 1)

    out = proc.stdout.strip()
    if out:
        try:
            result = json.loads(out.splitlines()[-1])
            result.setdefault("elapsed_s", elapsed_s)
            result.setdefault("strategy_file", strategy_file)
            if result.get("ok") and elapsed_s >= _SLOW_RUN_S:
                result.setdefault("warnings", []).append(
                    f"This backtest took ~{round(elapsed_s / 60)} min. For faster "
                    f"iteration, validate on a shorter window and reuse a data_dir "
                    f"before widening to a long run."
                )
            return result
        except json.JSONDecodeError:
            pass
    return {
        "error": "RunnerFailed",
        "message": (proc.stderr or out or "no output").strip()[-2000:],
        "elapsed_s": elapsed_s,
        "strategy_file": strategy_file,
    }


def build_server() -> FastMCP:
    mcp = FastMCP("marketlens", instructions=_INSTRUCTIONS)

    # ── Markets / events / series ─────────────────────────────────

    @mcp.tool()
    @_safe
    def search_markets(
        q: str | None = None,
        status: _Status | None = None,
        category: str | None = None,
        series_id: str | None = None,
        event_id: str | None = None,
        platform: str | None = None,
        min_volume: float | None = None,
        min_liquidity: float | None = None,
        sort: str | None = None,
        limit: int = 20,
    ) -> list[dict] | dict:
        """Search prediction markets. Returns compact rows; the `id` field is
        the market UUID used by get_market / get_orderbook / get_trades.

        q: full-text question search.
        sort: -volume, -liquidity, close_time, -created_at, resolved_at
        (prefix with - for descending, e.g. -created_at = newest first).
        Returns up to `limit` (max 200) rows.
        """
        params: dict[str, Any] = {"take": _clamp(limit)}
        for k, v in (
            ("q", q), ("status", status), ("category", category),
            ("series_id", series_id), ("event_id", event_id), ("platform", platform),
            ("min_volume", min_volume), ("min_liquidity", min_liquidity), ("sort", sort),
        ):
            if v is not None:
                params[k] = v
        return [fmt.market_brief(m) for m in _get_client().markets.list(**params)]

    @mcp.tool()
    @_safe
    def get_market(market_id: str) -> dict:
        """Get a single market by its UUID, with full detail."""
        return fmt.market_full(_get_client().markets.get(market_id))

    @mcp.tool()
    @_safe
    def search_events(
        q: str | None = None,
        category: str | None = None,
        series_id: str | None = None,
        platform: str | None = None,
        sort: str | None = None,
        limit: int = 20,
    ) -> list[dict] | dict:
        """Search events (groupings of related markets). q needs >= 3 chars."""
        params: dict[str, Any] = {"take": _clamp(limit)}
        for k, v in (
            ("q", q), ("category", category), ("series_id", series_id),
            ("platform", platform), ("sort", sort),
        ):
            if v is not None:
                params[k] = v
        return [fmt.event_brief(e) for e in _get_client().events.list(**params)]

    @mcp.tool()
    @_safe
    def search_series(
        category: str | None = None,
        platform: str | None = None,
        recurrence: str | None = None,
        sort: str | None = None,
        limit: int = 20,
    ) -> list[dict] | dict:
        """List recurring series (e.g. btc-up-or-down-5m). recurrence: 5m, 1h, daily, ..."""
        params: dict[str, Any] = {"take": _clamp(limit)}
        for k, v in (
            ("category", category), ("platform", platform),
            ("recurrence", recurrence), ("sort", sort),
        ):
            if v is not None:
                params[k] = v
        return [fmt.series_brief(s) for s in _get_client().series.list(**params)]

    # ── Order book ────────────────────────────────────────────────

    @mcp.tool()
    @_safe
    def get_orderbook(
        market_id: str, at: str | int | None = None, depth: int = 10,
    ) -> dict:
        """Reconstruct the L2 order book for a market UUID at a point in time.

        at: epoch ms or ISO 8601 (latest if omitted). depth: levels per side.
        `empty` is true only when there are no resting orders; `two_sided` is
        true when both sides have levels. A side with no orders reports its best
        price as null, and midpoint/spread/analytics are null unless two-sided.
        When two-sided it also returns spread_bps, microprice, and imbalance:
        top-3-level (bid_depth - ask_depth)/(bid_depth + ask_depth) in [-1, 1],
        where positive means more resting size on the bid side.
        """
        book = _get_client().orderbook.get(market_id, at=at, depth=depth)
        return fmt.book_view(book, depth=depth)

    @mcp.tool()
    @_safe
    def get_orderbook_metrics(
        market_id: str,
        after: str | int,
        before: str | int,
        resolution: _MetricRes = "1m",
        order: _Order = "asc",
        limit: int = 200,
    ) -> list[dict] | dict:
        """Time-bucketed order book metrics (best bid/ask, spread, depth) for a
        market UUID, a budget-friendly way to see how the book evolved.

        `after`/`before` (epoch ms or ISO 8601) are required. Each resolution
        covers a span up to 1m: 24h, 5m: 7d, 15m: 14d, 1h: 30d, 4h: 90d, 1d: 1y.
        """
        rows = _get_client().orderbook.metrics(
            market_id, after=after, before=before,
            resolution=resolution, order=order, take=_clamp(limit),
        )
        return [fmt.metric_row(m) for m in rows]

    @mcp.tool()
    @_safe
    def get_trades(
        market_id: str,
        after: str | int,
        before: str | int,
        side: _Side | None = None,
        min_size: float | None = None,
        order: _Order = "asc",
        limit: int = 200,
    ) -> list[dict] | dict:
        """List executed trades for a market UUID.

        `after`/`before` (epoch ms or ISO 8601) are required. Each returned
        trade bills one event against the daily budget, so keep the window tight.
        """
        params: dict[str, Any] = {
            "after": after, "before": before, "order": order, "take": _clamp(limit),
        }
        if side is not None:
            params["side"] = side
        if min_size is not None:
            params["min_size"] = min_size
        rows = _get_client().markets.trades(market_id, **params)
        return [fmt.trade_row(t) for t in rows]

    @mcp.tool()
    @_safe
    def get_candles(
        market_id: str,
        after: str | int,
        before: str | int,
        resolution: _CandleRes = "1m",
        order: _Order = "asc",
        limit: int = 200,
    ) -> list[dict] | dict:
        """OHLCV candles for a market UUID.

        `after`/`before` (epoch ms or ISO 8601) are required. Bills one event
        per returned candle.
        """
        rows = _get_client().markets.candles(
            market_id, after=after, before=before,
            resolution=resolution, order=order, take=_clamp(limit),
        )
        return [fmt.candle_row(c) for c in rows]

    @mcp.tool()
    @_safe
    def get_reference_candles(
        symbol: str,
        after: str | int,
        before: str | int,
        resolution: _CandleRes = "1m",
        order: _Order = "asc",
        limit: int = 200,
    ) -> list[dict] | dict:
        """Binance spot OHLCV for an underlying symbol (BTC, ETH, SOL, ...).

        `after`/`before` (epoch ms or ISO 8601) are required. Bills one event
        per returned candle.
        """
        rows = _get_client().reference.candles(
            symbol, after=after, before=before,
            resolution=resolution, order=order, take=_clamp(limit),
        )
        return [fmt.reference_candle_row(c) for c in rows]

    # ── Signals / surfaces ────────────────────────────────────────

    @mcp.tool()
    @_safe
    def get_signals(
        underlying: str | None = None,
        surface_type: _SurfaceType | None = None,
        limit: int = 50,
    ) -> list[dict] | dict:
        """List latest implied-probability surfaces (stats only, no per-strike array).

        underlying: BTC, ETH, ... Use get_surface with a row's series_id +
        event_id for the per-strike detail.
        """
        params: dict[str, Any] = {"take": _clamp(limit)}
        if underlying is not None:
            params["underlying"] = underlying
        if surface_type is not None:
            params["surface_type"] = surface_type
        return [fmt.surface_brief(s) for s in _get_client().signals.surfaces(**params)]

    @mcp.tool()
    @_safe
    def get_surface(series_id: str, event_id: str) -> dict:
        """Get the latest implied-probability surface for a series/event, with per-strike probabilities.

        Summary stats: implied_mean is the probability-weighted level in the
        underlying's price units; implied_cv is the coefficient of variation as
        a percent (std/mean x 100, so 5.6 means about +/-5.6%); implied_skew is
        the standardized third moment (dimensionless, positive = upside skew).
        For barrier surfaces these are null and implied_peak/implied_trough are
        populated instead. Each strike carries a raw and a fitted/normalized
        probability.
        """
        return fmt.surface_full(_get_client().signals.surface(series_id, event_id))

    # ── Strategy authoring + backtest ─────────────────────────────

    @mcp.tool()
    def strategy_reference() -> str:
        """Return the Strategy / StrategyContext API to author a backtest with.

        Read this before calling run_backtest so the code uses the real hooks
        and methods.
        """
        return _STRATEGY_REFERENCE

    @mcp.tool()
    def run_backtest(
        strategy_code: str,
        target_id: str | list[str],
        initial_cash: float = 10_000,
        after: str | int | None = None,
        before: str | int | None = None,
        fees: str | None = "polymarket",
        latency_ms: int = 50,
        slippage_bps: int = 0,
        limit_fill_rate: float = 0.1,
        queue_position: bool = False,
        auto_merge: bool = True,
        strategy_class: str | None = None,
        data_dir: str | None = None,
        save: bool = True,
        artifacts_dir: str = _DEFAULT_ARTIFACTS,
        label: str = "backtest",
        timeout_s: int = 600,
    ) -> dict:
        """Define and backtest an agent-authored strategy (see strategy_reference).

        target_id: a market UUID, series slug, or condition id, or a list of them
        to share one bankroll across a multi-asset portfolio. Pass after/before to
        bound a series run. Returns metrics, a by_series PnL breakdown, elapsed_s,
        any warnings, the strategy_file path, and (when save) a saved-result path
        you reopen with open_backtest. Pick a descriptive `label`: it names the
        saved strategy file (<label>.py) and the result directory.

        Validate on a SHORT window first, then widen deliberately: runtime grows
        with the data in the window, and a run is capped at timeout_s (default
        600s). Reuse a data_dir to cache a window for fast re-runs. limit_fill_rate
        applies when queue_position is False; queue_position instead models fill
        priority from the order queue. auto_merge (default True) nets matched
        YES+NO pairs back to cash after each fill; set it False to hold the two
        legs independently. Use compare_backtests to score several strategies
        over one window.
        """
        if os.environ.get("MARKETLENS_MCP_DISABLE_BACKTEST", "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            return {
                "error": "BacktestDisabled",
                "message": "run_backtest is disabled via MARKETLENS_MCP_DISABLE_BACKTEST.",
            }

        if isinstance(target_id, str) and any(c in target_id for c in ",;|"):
            return {
                "error": "InvalidParameter",
                "message": "target_id looks like a delimited string.",
                "hint": "For a multi-asset portfolio pass a JSON list of ids, "
                        'e.g. ["btc-up-or-down-5m", "eth-up-or-down-5m"], not a '
                        "comma-separated string.",
            }

        artifacts = Path(artifacts_dir)
        artifacts.mkdir(parents=True, exist_ok=True)
        # Name the strategy file after the model-chosen label so the authored
        # code is easy to find and reopen, rather than a random temp id.
        code_path = str(artifacts / f"{label}.py")
        Path(code_path).write_text(strategy_code)

        cfg = {
            "code_path": code_path,
            "strategy_class": strategy_class,
            "target_id": target_id,
            "after": after,
            "before": before,
            "initial_cash": initial_cash,
            "fees": fees,
            "latency_ms": latency_ms,
            "slippage_bps": slippage_bps,
            "limit_fill_rate": limit_fill_rate,
            "queue_position": queue_position,
            "auto_merge": auto_merge,
            "data_dir": data_dir,
            "save": save,
            "artifact_path": str(artifacts / label),
        }
        return _invoke_runner(cfg, timeout_s, code_path)

    @mcp.tool()
    def compare_backtests(
        variants: list[dict],
        target_id: str | list[str],
        initial_cash: float = 10_000,
        after: str | int | None = None,
        before: str | int | None = None,
        fees: str | None = "polymarket",
        latency_ms: int = 50,
        slippage_bps: int = 0,
        limit_fill_rate: float = 0.1,
        queue_position: bool = False,
        data_dir: str | None = None,
        save: bool = True,
        artifacts_dir: str = _DEFAULT_ARTIFACTS,
        label: str = "compare",
        timeout_s: int = 900,
    ) -> dict:
        """Score several strategies over the SAME target, window, and config and
        return their metrics side by side. The data is downloaded once and shared
        across variants, so this is a fair comparison that is cheaper than
        separate runs.

        `variants` is a list of at least two
        {"label": str, "strategy_code": str, "strategy_class"?: str}; author each
        per strategy_reference. The other params are shared and match run_backtest.
        Returns a `results` list, one {label, metrics, by_series} per variant.
        """
        if os.environ.get("MARKETLENS_MCP_DISABLE_BACKTEST", "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            return {
                "error": "BacktestDisabled",
                "message": "Backtesting is disabled via MARKETLENS_MCP_DISABLE_BACKTEST.",
            }
        if not isinstance(variants, list) or len(variants) < 2:
            return {
                "error": "InvalidParameter",
                "message": "Provide at least two variants, each with a label and strategy_code.",
            }

        artifacts = Path(artifacts_dir)
        artifacts.mkdir(parents=True, exist_ok=True)
        strategies = []
        for i, v in enumerate(variants):
            if not isinstance(v, dict) or "strategy_code" not in v:
                return {
                    "error": "InvalidParameter",
                    "message": f"variant {i} must be an object with a 'strategy_code' field.",
                }
            vlabel = str(v.get("label") or f"variant_{i + 1}")
            code_path = str(artifacts / f"{vlabel}.py")
            Path(code_path).write_text(v["strategy_code"])
            strategies.append({
                "label": vlabel,
                "code_path": code_path,
                "strategy_class": v.get("strategy_class"),
            })

        # Default to a per-comparison data_dir so the shared data downloads once.
        cache = data_dir or str(artifacts / f"{label}_data")
        cfg = {
            "strategies": strategies,
            "target_id": target_id,
            "after": after,
            "before": before,
            "initial_cash": initial_cash,
            "fees": fees,
            "latency_ms": latency_ms,
            "slippage_bps": slippage_bps,
            "limit_fill_rate": limit_fill_rate,
            "queue_position": queue_position,
            "data_dir": cache,
            "save": save,
            "artifact_path": str(artifacts / label),
        }
        return _invoke_runner(cfg, timeout_s, str(artifacts))

    @mcp.tool()
    def open_backtest(artifact_dir: str) -> dict:
        """Load a saved backtest result (the `artifact` path from run_backtest /
        compare_backtests) and return its detail without re-running it: metrics,
        per-series PnL, a `trades` ledger of every fill (entries and exits), and
        a `settlements` ledger of positions held to resolution. A strategy that
        sells before resolution shows its activity in `trades`, not `settlements`.
        """
        from marketlens.backtest import BacktestResult
        from marketlens.mcp import _runner

        path = Path(artifact_dir)
        if not (path / "manifest.json").exists():
            # maybe a compare dir of sub-runs
            subs = sorted(p.name for p in path.glob("*") if (p / "manifest.json").exists())
            if subs:
                return {
                    "runs": subs,
                    "hint": f"This is a multi-run directory; open one of: {subs}",
                }
            return {"error": "NotFound", "message": f"No saved backtest at {artifact_dir}."}

        try:
            result = BacktestResult.load(path)
        except Exception as exc:  # noqa: BLE001
            return {"error": type(exc).__name__, "message": str(exc)}

        ledger = [
            {
                "market_id": s.market_id,
                "series_id": s.series_id,
                "side": s.side.value,
                "shares": s.shares,
                "avg_entry_price": s.avg_entry_price,
                "settlement_price": s.settlement_price,
                "pnl": s.pnl,
                "fees": s.fees,
                "winning_outcome": s.winning_outcome,
                "resolved_at": s.resolved_at,
            }
            for s in result._settlements
        ]
        trades = [
            {
                "market_id": f.market_id,
                "side": f.side.value,
                "price": f.price,
                "size": f.size,
                "fee": f.fee,
                "t": f.timestamp,
                "is_maker": f.is_maker,
            }
            for f in result._fills
        ]
        out = {
            "metrics": _runner._metrics(result),
            "by_series": _runner._by_series(result, _get_client()),
            "trades": trades,
            "settlements": ledger,
        }
        warnings = _runner._warnings(result)
        if warnings:
            out["warnings"] = warnings
        return out

    return mcp


_STRATEGY_REFERENCE = """\
Subclass marketlens.backtest.Strategy and override the hooks you need:

    from marketlens.backtest import Strategy

    class MyStrategy(Strategy):
        def on_market_start(self, ctx, market, book):
            self.entered = False
        def on_book(self, ctx, market, book):
            if not self.entered and book.bid_levels and book.ask_levels and book.midpoint < 0.45:
                ctx.buy_yes(size=200)
                self.entered = True

Hooks (override any subset):
  on_market_start(ctx, market, book)  - a new market begins
  on_book(ctx, market, book)          - every book change (snapshot/delta)
  on_trade(ctx, market, book, trade)  - every historical trade
  on_fill(ctx, market, fill)          - an order fills; fill has .side, .size, .price, .fee, .is_maker
  on_reject(ctx, market, order)       - an order is rejected
  on_market_end(ctx, market)          - market data exhausted, before settlement

ctx:
  buy_yes / buy_no / sell_yes / sell_no(size, *, market_id=None, limit_price=None,
      cancel_after=None) -> Order      # limit_price omitted = market order
  cancel(order), cancel_all(market_id=None)
  position(market_id=None) -> Position(side "YES"/"NO"/"FLAT", shares, avg_entry_price,
      cost_basis, unrealized_pnl, realized_pnl, total_fees)   # the field is .shares, not .size
  yes_position / no_position(market_id=None) -> Position   # one leg; net is position()
  split / merge(size, *, market_id=None)   # CTF mint/redeem: size YES+NO <-> size cash
  cash, equity, open_orders, book, market, time
  books -> dict[market_id, OrderBook]   # all active books
  reference_price(market_id=None) -> float | None   # ~1s Binance spot; a signal, not the resolution oracle

book: midpoint, best_bid, best_ask, spread, bid_levels, ask_levels (0 = that side empty),
  spread_bps(), microprice(), imbalance(levels=3), impact(side, size), slippage(side, size).
  Prices are floats in [0, 1].

Rules:
- Positions are long only: buy_* open or add, sell_* reduce (sell up to what you hold).
  To quote the ask on YES, buy_no at 1 - price. By default (auto_merge=True) matched
  YES+NO net back into $1 cash, so buy_yes(p) + buy_no(1-p) is a two-sided quote that
  earns the spread. Run with auto_merge=False to hold the two legs separately and read
  them with yes_position() / no_position().
- Fills lag by latency_ms, so position() is unchanged in the on_book that placed the
  order; react in on_fill or guard on position().shares.
- Unsold positions settle at resolution ($1 win / $0 lose). win_rate and holding metrics
  are settlement-based, so a strategy that exits before resolution can show 0 there even
  when total_pnl is positive; judge those on total_pnl / total_return.

Pass the code as strategy_code to run_backtest (target_id = a market UUID, series slug,
condition id, or a list for a portfolio; after/before for series).
"""


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
