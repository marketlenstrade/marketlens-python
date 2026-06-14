"""Token-lean shaping of SDK models for MCP tool output.

Tools return plain JSON-serializable dicts. List endpoints get a compact
``*_brief`` view that drops rarely-needed fields so an agent can scan many
rows cheaply; single-item lookups return the fuller view.
"""

from __future__ import annotations

import math
from typing import Any


def _num(v: Any) -> Any:
    """JSON-safe number: inf/-inf/nan become strings or None."""
    if isinstance(v, float):
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        if math.isnan(v):
            return None
    return v


def market_brief(m: Any) -> dict:
    return {
        "id": m.id,
        "question": m.question,
        "status": m.status,
        "category": m.category,
        "series_id": m.series_id,
        "series_title": m.series_title,
        "event_id": m.event_id,
        "underlying": m.underlying,
        "strike": m.strike,
        "volume": m.volume,
        "liquidity": m.liquidity,
        "open_time": m.open_time,
        "close_time": m.close_time,
        "resolved_at": m.resolved_at,
        "winning_outcome": m.winning_outcome,
        "outcomes": [{"name": o.name, "last_price": o.last_price} for o in m.outcomes],
    }


def market_full(m: Any) -> dict:
    return m.model_dump()


def event_brief(e: Any) -> dict:
    return {
        "id": e.id,
        "title": e.title,
        "category": e.category,
        "series_id": e.series_id,
        "series_title": getattr(e, "series_title", None),
        "market_count": e.market_count,
        "start_date": e.start_date,
        "end_date": e.end_date,
    }


def series_brief(s: Any) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "platform_series_id": getattr(s, "platform_series_id", None),
        "category": getattr(s, "category", None),
        "recurrence": getattr(s, "recurrence", None),
        "is_rolling": s.is_rolling,
        "structured_type": getattr(s, "structured_type", None),
        "market_count": s.market_count,
        "first_market_close": getattr(s, "first_market_close", None),
        "last_market_close": getattr(s, "last_market_close", None),
    }


def book_view(b: Any, *, depth: int) -> dict:
    """Order book with the top ``depth`` levels per side plus analytics.

    ``empty`` is true only when there are no resting orders at all.
    ``two_sided`` is true when both sides have levels. A side with no orders
    reports its ``best_*`` price as ``null`` (not a neutral placeholder that
    could read as a crossed quote), and midpoint/spread/analytics are ``null``
    unless the book is two-sided.
    """
    has_bids = bool(b.bid_levels)
    has_asks = bool(b.ask_levels)
    two_sided = has_bids and has_asks
    out: dict[str, Any] = {
        "market_id": b.market_id,
        "as_of": b.as_of,
        "empty": not (has_bids or has_asks),
        "two_sided": two_sided,
        "best_bid": b.best_bid if has_bids else None,
        "best_ask": b.best_ask if has_asks else None,
        "midpoint": b.midpoint if two_sided else None,
        "spread": b.spread if two_sided else None,
        "bid_depth": b.bid_depth,
        "ask_depth": b.ask_depth,
        "bid_levels": b.bid_levels,
        "ask_levels": b.ask_levels,
        "bids": [{"price": lv.price, "size": lv.size} for lv in b.bids[:depth]],
        "asks": [{"price": lv.price, "size": lv.size} for lv in b.asks[:depth]],
    }
    # Analytics need both sides; only include them when present.
    if two_sided:
        out["spread_bps"] = _num(b.spread_bps())
        out["microprice"] = _num(b.microprice())
        out["imbalance"] = _num(b.imbalance(levels=3))
    return out


def metric_row(m: Any) -> dict:
    return {
        "t": m.t,
        "best_bid": m.best_bid,
        "best_ask": m.best_ask,
        "midpoint": m.midpoint,
        "spread": m.spread,
        "bid_depth": m.bid_depth,
        "ask_depth": m.ask_depth,
        "bid_levels": m.bid_levels,
        "ask_levels": m.ask_levels,
    }


def trade_row(t: Any) -> dict:
    return {
        "id": t.id,
        "t": t.platform_timestamp,
        "price": t.price,
        "size": t.size,
        "side": t.side,
        "fee_rate_bps": t.fee_rate_bps,
    }


def candle_row(c: Any) -> dict:
    return {
        "open_time": c.open_time,
        "close_time": c.close_time,
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "vwap": c.vwap,
        "volume": c.volume,
        "trade_count": c.trade_count,
    }


def reference_candle_row(c: Any) -> dict:
    return {
        "t": getattr(c, "timestamp", None),
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": getattr(c, "volume", None),
    }


def surface_brief(s: Any) -> dict:
    """Surface stats without the per-strike array (use get_surface for that)."""
    return {
        "series_id": s.series_id,
        "event_id": s.event_id,
        "series_title": s.series_title,
        "surface_type": s.surface_type,
        "underlying": s.underlying,
        "computed_at": s.computed_at,
        "expiry_ms": s.expiry_ms,
        "n_strikes": s.n_strikes,
        "implied_mean": s.implied_mean,
        "implied_cv": s.implied_cv,
        "implied_skew": s.implied_skew,
        "implied_peak": s.implied_peak,
        "implied_trough": s.implied_trough,
    }


def surface_full(s: Any) -> dict:
    out = surface_brief(s)
    out["strikes"] = list(s.strikes)
    return out
