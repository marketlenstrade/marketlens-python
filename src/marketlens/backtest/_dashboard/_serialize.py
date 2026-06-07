from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from marketlens.backtest._results import BacktestResult

_MAX_EQUITY_POINTS = 2000
_MAX_MARKERS = 3000  # cap trade/order timeline markers so the browser stays responsive


def serialize_results(
    results: list[BacktestResult],
    labels: list[str] | None = None,
    title: str | None = None,
) -> dict:
    if labels is None:
        labels = [f"Run {i + 1}" for i in range(len(results))]
    data: dict = {"runs": [_serialize_one(r, l) for r, l in zip(results, labels)]}
    if title is not None:
        data["title"] = title
    return data


def _serialize_one(result: BacktestResult, label: str) -> dict:
    names = getattr(result, "market_names", {}) or {}
    return {
        "label": label,
        "market_names": names,
        "metrics": _serialize_metrics(result),
        "equity_curve": _serialize_equity(result._equity_curve),
        "drawdown_curve": _compute_drawdown(result._equity_curve),
        "trades": _serialize_trades(result._fills),
        "orders": _serialize_orders(result._orders),
        "settlements": _serialize_settlements(result._settlements),
        "pnl_by_market": _aggregate_pnl_by_market(result._settlements, names),
        "order_stats": _compute_order_stats(result._orders, result._fills),
        "config": _serialize_config(result),
    }


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    f = float(val)
    if f != f or f == float("inf") or f == float("-inf"):
        return None
    return f


def _serialize_metrics(r: BacktestResult) -> dict:
    return {
        "total_pnl": _safe_float(r.total_pnl),
        "total_return": _safe_float(r.total_return),
        "sharpe_ratio": _safe_float(r.sharpe_ratio),
        "sortino_ratio": _safe_float(r.sortino_ratio),
        "max_drawdown": _safe_float(r.max_drawdown),
        "max_drawdown_duration": r.max_drawdown_duration,
        "win_rate": _safe_float(r.win_rate),
        "profit_factor": _safe_float(r.profit_factor),
        "expectancy": _safe_float(r.expectancy),
        "avg_win": _safe_float(r.avg_win),
        "avg_loss": _safe_float(r.avg_loss),
        "payoff_ratio": _safe_float(r.payoff_ratio),
        "avg_holding_ms": r.avg_holding_ms,
        "capital_utilization": _safe_float(r.capital_utilization),
        "total_trades": r.total_trades,
        "markets_traded": r.markets_traded,
        "total_fees": _safe_float(r.total_fees),
        "fee_drag_bps": _safe_float(r.fee_drag_bps),
        "avg_entry_price": _safe_float(r.avg_entry_price),
        "cash_rejected": r.cash_rejected,
        "initial_cash": _safe_float(r.initial_cash),
    }


def _serialize_equity(curve: list[dict]) -> list[dict]:
    pts = _downsample(curve, _MAX_EQUITY_POINTS)
    return [
        {
            "t": p["t"],
            "cash": float(p["cash"]),
            "equity": float(p["equity"]),
            "pnl": float(p["pnl"]),
        }
        for p in pts
    ]


def _compute_drawdown(curve: list[dict]) -> list[dict]:
    if not curve:
        return []
    pts = _downsample(curve, _MAX_EQUITY_POINTS)
    peak = float(pts[0]["equity"])
    result = []
    for p in pts:
        eq = float(p["equity"])
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak if peak > 0 else 0.0
        result.append({"t": p["t"], "drawdown": dd})
    return result


def _downsample(data: list, max_points: int) -> list:
    if len(data) <= max_points:
        return data
    step = len(data) / max_points
    indices = [int(i * step) for i in range(max_points)]
    if indices[-1] != len(data) - 1:
        indices[-1] = len(data) - 1
    return [data[i] for i in indices]


def _serialize_trades(fills: list) -> list[dict]:
    # Cap the timeline markers; active strategies emit tens of thousands of fills
    # which would crash the browser. Headline counts come from the full lists.
    fills = _downsample(fills, _MAX_MARKERS)
    return [
        {
            "t": f.timestamp,
            "market_id": f.market_id,
            "side": f.side.value,
            "price": float(f.price),
            "size": float(f.size),
            "fee": float(f.fee),
            "is_maker": f.is_maker,
        }
        for f in fills
    ]


def _serialize_orders(orders: list) -> list[dict]:
    orders = _downsample(orders, _MAX_MARKERS)
    return [
        {
            "t": o.submitted_at,
            "market_id": o.market_id,
            "side": o.side.value,
            "order_type": o.order_type.value,
            "size": float(o.size),
            "limit_price": float(o.limit_price) if o.limit_price is not None else None,
            "status": o.status.value,
            "filled_size": float(o.filled_size),
            "avg_fill_price": (
                float(o.avg_fill_price) if o.avg_fill_price is not None else None
            ),
            "total_fees": float(o.total_fees),
        }
        for o in orders
    ]


def _serialize_settlements(settlements: list) -> list[dict]:
    return [
        {
            "market_id": s.market_id,
            "series_id": s.series_id,
            "side": s.side.value if hasattr(s.side, "value") else str(s.side),
            "shares": float(s.shares),
            "avg_entry_price": float(s.avg_entry_price),
            "settlement_price": float(s.settlement_price),
            "pnl": float(s.pnl),
            "fees": float(s.fees),
            "net_pnl": float(s.pnl) - float(s.fees),
            "winning_outcome": s.winning_outcome,
            "resolved_at": s.resolved_at,
        }
        for s in settlements
    ]


def _aggregate_pnl_by_market(
    settlements: list, names: dict[str, str] | None = None,
) -> list[dict]:
    names = names or {}
    by_market: dict[str, dict] = defaultdict(
        lambda: {"gross_pnl": 0.0, "fees": 0.0}
    )
    for s in settlements:
        m = by_market[s.market_id]
        m["gross_pnl"] += float(s.pnl)
        m["fees"] += float(s.fees)
    return sorted(
        [
            {
                "market_id": mid,
                "name": names.get(mid),
                "net_pnl": v["gross_pnl"] - v["fees"],
                "gross_pnl": v["gross_pnl"],
                "fees": v["fees"],
            }
            for mid, v in by_market.items()
        ],
        key=lambda x: x["net_pnl"],
        reverse=True,
    )


def _compute_order_stats(orders: list, fills: list) -> dict:
    total = len(orders)
    by_status: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    for o in orders:
        by_status[o.status.value] += 1
        by_type[o.order_type.value] += 1

    filled = sum(1 for o in orders if o.filled_size and float(o.filled_size) > 0)
    maker_count = sum(1 for f in fills if f.is_maker)

    return {
        "total": total,
        "by_status": dict(by_status),
        "by_type": dict(by_type),
        "fill_rate": filled / total if total > 0 else 0.0,
        "maker_pct": maker_count / len(fills) if fills else 0.0,
    }


def _serialize_config(result: BacktestResult) -> dict | None:
    cfg = result.config
    if cfg is None:
        return None
    d: dict[str, Any] = {}
    d["initial_cash"] = result.initial_cash
    for field in (
        "taker_only",
        "max_fill_fraction",
        "latency_ms",
        "slippage_bps",
        "limit_fill_rate",
        "queue_position",
        "settlement_delay_ms",
    ):
        if hasattr(cfg, field):
            d[field] = getattr(cfg, field)
    if hasattr(cfg, "fees") and cfg.fees is not None:
        d["fees"] = str(cfg.fees)
    return d
