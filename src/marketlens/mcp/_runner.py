"""Subprocess entry point that runs an agent-authored backtest.

Invoked as ``python -m marketlens.mcp._runner`` by the ``run_backtest`` MCP
tool. Reads a JSON config from stdin, imports the user's strategy module from
a file on disk (so tracebacks carry real line numbers), runs the backtest, and
prints a single JSON result line to stdout. Engine progress goes to stderr.

Running in a separate process keeps the long-lived MCP server clean and lets
the parent enforce a hard timeout on untrusted strategy code.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import traceback
from typing import Any


def _json_num(v: Any) -> Any:
    if isinstance(v, float):
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        if math.isnan(v):
            return None
    return v


def _metrics(result: Any) -> dict:
    keys = (
        "total_pnl", "total_return", "win_rate", "sharpe_ratio", "sortino_ratio",
        "max_drawdown", "profit_factor", "expectancy", "avg_win", "avg_loss",
        "payoff_ratio", "total_trades", "markets_traded", "total_fees",
        "fee_drag_bps", "capital_utilization", "avg_holding_ms",
    )
    out = {k: _json_num(getattr(result, k, None)) for k in keys}
    out["total_orders"] = len(getattr(result, "_orders", None) or [])
    out["cash_rejected"] = getattr(result, "cash_rejected", 0)
    return out


def _series_labels(client: Any, ids: Any) -> dict:
    """Resolve series UUIDs to {slug, title} so a by_series breakdown is
    self-describing. Best-effort: a failed lookup just yields no label."""
    labels: dict = {}
    for sid in {s for s in ids if s}:
        try:
            s = client.series.get(sid)
            labels[sid] = {"slug": s.platform_series_id, "title": s.title}
        except Exception:
            labels[sid] = {}
    return labels


def _by_series(result: Any, client: Any = None) -> dict | None:
    """Per-series PnL attribution (which asset made/lost money in a portfolio),
    annotated with the series slug/title when a client is available."""
    try:
        breakdown = result.by_series()
    except Exception:
        return None
    labels = _series_labels(client, breakdown.keys()) if client is not None else {}
    out: dict = {}
    for sid, stats in breakdown.items():
        row = {k: _json_num(v) for k, v in stats.items()}
        lab = labels.get(sid) or {}
        if lab.get("slug"):
            row["series_slug"] = lab["slug"]
        if lab.get("title"):
            row["series_title"] = lab["title"]
        out[str(sid)] = row
    return out


def _warnings(result: Any) -> list:
    """Flag degenerate or pathological runs that otherwise look like clean
    successes (no trades, runaway order loops, capital eaten by fees)."""
    w: list = []
    trades = getattr(result, "total_trades", 0) or 0
    markets = getattr(result, "markets_traded", 0) or 0
    orders = len(getattr(result, "_orders", None) or [])
    rejected = getattr(result, "cash_rejected", 0) or 0
    fees = getattr(result, "total_fees", 0.0) or 0.0
    cash = getattr(result, "initial_cash", 0.0) or 0.0

    if trades == 0:
        w.append(
            "0 trades: the strategy never filled an order in this window. Check the "
            "entry condition, the window, or that the book was two-sided."
        )
    if markets and orders / markets > 100:
        w.append(
            f"High order rate: {orders} orders across {markets} markets "
            f"(~{orders // markets}/market), a possible runaway order loop."
        )
    if orders and rejected / orders > 0.1:
        w.append(
            f"{rejected} orders ({rejected * 100 // orders}%) were rejected for "
            f"insufficient cash."
        )
    if cash and fees > 0.25 * cash:
        w.append(
            f"Fees ({fees:.0f}) consumed {fees * 100 / cash:.0f}% of initial "
            f"capital ({cash:.0f})."
        )
    return w


def _config_kwargs(cfg: dict) -> dict:
    return dict(
        after=cfg.get("after"),
        before=cfg.get("before"),
        initial_cash=cfg["initial_cash"],
        fees=cfg.get("fees", "polymarket"),
        latency_ms=cfg.get("latency_ms", 50),
        slippage_bps=cfg.get("slippage_bps", 0),
        limit_fill_rate=cfg.get("limit_fill_rate", 0.1),
        queue_position=cfg.get("queue_position", False),
        data_dir=cfg.get("data_dir"),
        progress=False,
    )


def _load_strategy(code_path: str, class_name: str | None) -> Any:
    from marketlens.backtest import Strategy

    spec = importlib.util.spec_from_file_location("ml_user_strategy", code_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load strategy module from {code_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ml_user_strategy"] = module
    spec.loader.exec_module(module)

    candidates = [
        v for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy
    ]
    if class_name:
        for c in candidates:
            if c.__name__ == class_name:
                return c
        raise RuntimeError(
            f"No Strategy subclass named {class_name!r} found in the supplied code."
        )
    if not candidates:
        raise RuntimeError(
            "No Strategy subclass found. Define a subclass of "
            "marketlens.backtest.Strategy in the supplied code."
        )
    if len(candidates) > 1:
        names = ", ".join(c.__name__ for c in candidates)
        raise RuntimeError(
            f"Multiple Strategy subclasses found ({names}). "
            f"Pass strategy_class to pick one."
        )
    return candidates[0]


def _error_payload(exc: Exception) -> dict:
    payload: dict[str, Any] = {
        "ok": False,
        "error": type(exc).__name__,
        "message": str(exc),
    }
    try:
        from marketlens.exceptions import MarketLensError
        expected = isinstance(exc, MarketLensError)
    except Exception:
        expected = False

    # The export for a recent window may still be building server-side. Point
    # the caller at the fix instead of leaving "status=pending" cryptic.
    if type(exc).__name__ == "ExportNotReadyError":
        payload["hint"] = (
            "One or more markets in this window have no built export yet "
            "(status=pending). Exports build progressively and can be "
            "non-contiguous across a window, so neither older nor newer is "
            "reliably safe: narrow to a window you know is fully settled, or "
            "retry later."
        )

    # Expected API conditions (budget, not-ready, bad param) are self-explanatory
    # from the message; skip the noisy SDK-internal traceback. Keep the full
    # traceback for unexpected failures, which are usually bugs in the strategy
    # code the caller needs to debug.
    if not expected:
        payload["traceback"] = traceback.format_exc()
    return payload


def _result_block(result: Any, client: Any) -> dict:
    """The metrics/by_series/warnings shared by single and multi runs."""
    block: dict[str, Any] = {
        "metrics": _metrics(result),
        "by_series": _by_series(result, client),
    }
    warnings = _warnings(result)
    if warnings:
        block["warnings"] = warnings
    return block


def _run_multi(cfg: dict, client: Any) -> dict:
    """Run several strategies over the same target/window/config and return
    each one's metrics, for a fair side-by-side comparison."""
    strategies, labels = [], []
    for v in cfg["strategies"]:
        cls = _load_strategy(v["code_path"], v.get("strategy_class"))
        strategies.append(cls())
        labels.append(v["label"])
    multi = client.backtest(strategies, cfg["target_id"], labels=labels, **_config_kwargs(cfg))
    out: dict[str, Any] = {
        "ok": True,
        "results": [
            {"label": lbl, **_result_block(res, client)}
            for lbl, res in zip(multi.labels, multi.results)
        ],
    }
    if cfg.get("save"):
        out["artifact"] = str(multi.save(cfg["artifact_path"], overwrite=True))
    return out


def main() -> None:
    cfg = json.loads(sys.stdin.read())
    try:
        from marketlens import MarketLens

        client = MarketLens()
        if cfg.get("strategies"):
            out = _run_multi(cfg, client)
        else:
            strat_cls = _load_strategy(cfg["code_path"], cfg.get("strategy_class"))
            result = client.backtest(strat_cls(), cfg["target_id"], **_config_kwargs(cfg))
            out = {"ok": True, **_result_block(result, client)}
            if cfg.get("save"):
                out["artifact"] = str(result.save(cfg["artifact_path"], overwrite=True))
        sys.stdout.write(json.dumps(out))
    except Exception as exc:  # noqa: BLE001  report any failure as structured JSON
        sys.stdout.write(json.dumps(_error_payload(exc)))


if __name__ == "__main__":
    main()
