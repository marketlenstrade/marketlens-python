"""Client-side implied probability surface computation from walk state.

Produces ``Surface`` objects matching the server API format,
computed from live order book midpoints during a walk.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from marketlens.types.signal import Surface

if TYPE_CHECKING:
    from marketlens.types.event import Event
    from marketlens.types.market import Market
    from marketlens.types.orderbook import OrderBook
    from marketlens.types.series import Series

# ── Underlying extraction ────────────────────────────────────────

_NAME_TO_SYMBOL: dict[str, str] = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
    "xrp": "XRP",
    "apple": "AAPL", "meta": "META", "tesla": "TSLA",
    "nvidia": "NVDA", "amazon": "AMZN", "google": "GOOGL",
    "alphabet": "GOOGL", "microsoft": "MSFT", "netflix": "NFLX",
    "palantir": "PLTR", "opendoor": "OPEN",
    "gold": "GOLD", "chainlink": "LINK",
    "hyperliquid": "HYPE", "bnb": "BNB", "ethena": "ENA",
    "dogecoin": "DOGE", "doge": "DOGE", "lighter": "LIGHTER",
}

_RE_TICKER_PARENS = re.compile(r"\(([A-Z]{1,5})\)")


def _extract_underlying(question: str) -> str | None:
    m = _RE_TICKER_PARENS.search(question)
    if m:
        return m.group(1)
    q_lower = question.lower()
    for name, symbol in _NAME_TO_SYMBOL.items():
        if re.search(rf"\b{re.escape(name)}\b", q_lower):
            return symbol
    return None


# ── PAVA (Pool Adjacent Violators Algorithm) ─────────────────────


def _pava_decreasing(values: list[float]) -> list[float]:
    """Isotonic regression enforcing a monotone-decreasing constraint."""
    n = len(values)
    if n <= 1:
        return list(values)

    # Each block: [weighted_sum, weight, start_idx, end_idx]
    blocks: list[list[float]] = []
    for i, v in enumerate(values):
        blocks.append([v, 1.0, float(i), float(i)])
        while len(blocks) >= 2:
            prev, curr = blocks[-2], blocks[-1]
            if curr[0] / curr[1] > prev[0] / prev[1]:
                prev[0] += curr[0]
                prev[1] += curr[1]
                prev[3] = curr[3]
                blocks.pop()
            else:
                break

    result = [0.0] * n
    for weighted_sum, weight, start, end in blocks:
        avg = weighted_sum / weight
        for i in range(int(start), int(end) + 1):
            result[i] = avg
    return result


# ── Survival moments ─────────────────────────────────────────────


def _survival_moments(
    sorted_strikes: list[float], fitted: list[float],
) -> tuple[float | None, float | None, float | None]:
    """Compute implied mean, CV, and skew from a survival curve."""
    n = len(sorted_strikes)
    if n < 2:
        return None, None, None

    implied_mean = sorted_strikes[0]
    for i in range(n - 1):
        dk = sorted_strikes[i + 1] - sorted_strikes[i]
        implied_mean += (fitted[i] + fitted[i + 1]) / 2 * dk

    left_tail = 1.0 - fitted[0]
    right_tail = fitted[-1]

    variance = 0.0
    if left_tail > 0:
        variance += left_tail * (sorted_strikes[0] - implied_mean) ** 2
    for i in range(n - 1):
        p_i = max(fitted[i] - fitted[i + 1], 0)
        mid = (sorted_strikes[i] + sorted_strikes[i + 1]) / 2
        variance += p_i * (mid - implied_mean) ** 2
    if right_tail > 0:
        variance += right_tail * (sorted_strikes[-1] - implied_mean) ** 2

    std_dev = math.sqrt(variance) if variance > 0 else 0
    implied_cv = (std_dev / implied_mean * 100) if implied_mean > 0 else None

    implied_skew = None
    if std_dev > 0:
        skew = 0.0
        if left_tail > 0:
            skew += left_tail * ((sorted_strikes[0] - implied_mean) / std_dev) ** 3
        for i in range(n - 1):
            p_i = max(fitted[i] - fitted[i + 1], 0)
            mid = (sorted_strikes[i] + sorted_strikes[i + 1]) / 2
            skew += p_i * ((mid - implied_mean) / std_dev) ** 3
        if right_tail > 0:
            skew += right_tail * ((sorted_strikes[-1] - implied_mean) / std_dev) ** 3
        implied_skew = skew

    return implied_mean, implied_cv, implied_skew


# ── Surface builders ─────────────────────────────────────────────


def _compute_survival(
    entries: list[tuple[str, float, float]],
) -> tuple[list[dict], float | None, float | None, float | None]:
    """Compute survival surface from (market_id, strike, midpoint) tuples."""
    combined = sorted(entries, key=lambda x: x[1])
    sorted_strikes = [c[1] for c in combined]
    sorted_raw = [c[2] for c in combined]
    sorted_ids = [c[0] for c in combined]

    fitted = _pava_decreasing(sorted_raw)

    strike_data = [
        {
            "strike": sorted_strikes[i],
            "raw_prob": round(sorted_raw[i], 6),
            "fitted_prob": round(fitted[i], 6),
            "market_id": sorted_ids[i],
        }
        for i in range(len(sorted_strikes))
    ]

    implied_mean, implied_cv, implied_skew = _survival_moments(sorted_strikes, fitted)
    return strike_data, implied_mean, implied_cv, implied_skew


def _compute_density(
    entries: list[tuple[str, float, float | None, str, float]],
) -> tuple[list[dict], float | None, float | None, float | None]:
    """Compute density surface from (market_id, strike, strike_upper, direction, midpoint)."""
    buckets: list[dict] = []
    for market_id, strike, strike_upper, direction, midpoint in entries:
        if direction == "between" and strike_upper is not None:
            buckets.append({
                "lower": strike, "upper": strike_upper,
                "prob": round(midpoint, 6), "market_id": market_id,
            })
        elif direction == "below_tail":
            buckets.append({
                "lower": None, "upper": strike,
                "prob": round(midpoint, 6), "market_id": market_id,
            })
        elif direction == "above_tail":
            buckets.append({
                "lower": strike, "upper": None,
                "prob": round(midpoint, 6), "market_id": market_id,
            })

    if not buckets:
        return [], None, None, None

    total_prob = sum(b["prob"] for b in buckets)
    if total_prob <= 0:
        for b in buckets:
            b["normalized_prob"] = 0.0
        return buckets, None, None, None

    finite_bounds = [
        v for b in buckets
        for v in (b["lower"], b["upper"])
        if v is not None
    ]
    if len(finite_bounds) < 2:
        for b in buckets:
            b["normalized_prob"] = round(b["prob"] / total_prob, 6)
        return buckets, None, None, None

    min_bound, max_bound = min(finite_bounds), max(finite_bounds)
    range_width = max_bound - min_bound

    def bucket_center(b: dict) -> float:
        lo = b["lower"] if b["lower"] is not None else min_bound - range_width * 0.5
        hi = b["upper"] if b["upper"] is not None else max_bound + range_width * 0.5
        return (lo + hi) / 2

    for b in buckets:
        b["normalized_prob"] = round(b["prob"] / total_prob, 6)

    implied_mean = sum(bucket_center(b) * b["normalized_prob"] for b in buckets)

    variance = sum(
        b["normalized_prob"] * (bucket_center(b) - implied_mean) ** 2
        for b in buckets
    )
    std_dev = math.sqrt(variance) if variance > 0 else 0
    implied_cv = (std_dev / implied_mean * 100) if implied_mean > 0 else None

    implied_skew = None
    if std_dev > 0:
        implied_skew = sum(
            b["normalized_prob"] * ((bucket_center(b) - implied_mean) / std_dev) ** 3
            for b in buckets
        )

    return buckets, implied_mean, implied_cv, implied_skew


def _compute_barrier(
    entries: list[tuple[str, float, str, float]],
) -> tuple[list[dict], float | None, float | None, float | None, float | None]:
    """Compute barrier surface from (market_id, strike, direction, midpoint)."""
    upside: list[dict] = []
    downside: list[dict] = []

    for market_id, strike, direction, midpoint in entries:
        entry = {
            "strike": strike,
            "direction": direction,
            "raw_prob": round(midpoint, 6),
            "market_id": market_id,
        }
        if direction in ("reach", "hit_high"):
            upside.append(entry)
        elif direction in ("dip", "hit_low"):
            downside.append(entry)

    if upside:
        upside.sort(key=lambda x: x["strike"])
        fitted = _pava_decreasing([e["raw_prob"] for e in upside])
        for i, e in enumerate(upside):
            e["fitted_prob"] = round(fitted[i], 6)

    if downside:
        downside.sort(key=lambda x: -x["strike"])
        fitted = _pava_decreasing([e["raw_prob"] for e in downside])
        for i, e in enumerate(downside):
            e["fitted_prob"] = round(fitted[i], 6)

    all_strikes = upside + downside
    if not all_strikes:
        return [], None, None, None, None

    implied_peak = implied_peak_cv = None
    if len(upside) >= 2:
        reach_strikes = [e["strike"] for e in upside]
        reach_fitted = [e["fitted_prob"] for e in upside]
        implied_peak, implied_peak_cv, _ = _survival_moments(reach_strikes, reach_fitted)

    implied_trough = implied_trough_cv = None
    if len(downside) >= 2:
        dip_asc = sorted(downside, key=lambda x: x["strike"])
        dip_strikes = [e["strike"] for e in dip_asc]
        dip_survival = [1.0 - e["fitted_prob"] for e in dip_asc]
        implied_trough, implied_trough_cv, _ = _survival_moments(dip_strikes, dip_survival)

    return all_strikes, implied_peak, implied_peak_cv, implied_trough, implied_trough_cv


# ── Common helpers ───────────────────────────────────────────────


def _fmt(value: float | None, decimals: int) -> str | None:
    return str(round(value, decimals)) if value is not None else None


def _scan_books(
    books: dict[str, OrderBook],
    markets: dict[str, Market],
) -> tuple[list[tuple[Market, float]], int]:
    """Collect valid (market, midpoint) pairs and latest timestamp.

    Filters out markets without strikes and books with extreme
    or missing midpoints.
    """
    entries: list[tuple[Market, float]] = []
    computed_at = 0
    for market_id, book in books.items():
        market = markets.get(market_id)
        if not market or market.strike is None:
            continue
        # Empty book → ``midpoint`` falls back to the 0.5 neutral prior,
        # which would silently pollute the surface fit. Skip until both
        # sides have at least one level.
        if not book.bids or not book.asks:
            continue
        mp = book.midpoint
        if mp < 0.005 or mp > 0.995:
            continue
        entries.append((market, mp))
        if book.as_of and book.as_of > computed_at:
            computed_at = book.as_of
    return entries, computed_at


# ── Public API ───────────────────────────────────────────────────


def compute_surface(
    books: dict[str, OrderBook],
    markets: dict[str, Market],
    series: Series,
    event: Event | None = None,
) -> Surface | None:
    """Compute an implied probability surface from current walk state.

    Returns a ``Surface`` matching the API format, or ``None`` if the
    series is not structured or there isn't enough data.
    """
    surface_type = series.structured_type
    if not surface_type:
        return None

    underlying = None
    for market in markets.values():
        underlying = _extract_underlying(market.question)
        if underlying:
            break
    if not underlying:
        return None

    valid, computed_at = _scan_books(books, markets)
    min_strikes = 3 if surface_type == "density" else 2
    if len(valid) < min_strikes:
        return None

    common = dict(
        series_id=series.id,
        event_id=event.id if event else "",
        series_title=series.title,
        surface_type=surface_type,
        underlying=underlying,
        computed_at=computed_at,
        expiry_ms=(event.end_date or 0) if event else 0,
    )

    if surface_type == "survival":
        entries = [(m.id, float(m.strike), mp) for m, mp in valid]
        strike_data, mean, cv, skew = _compute_survival(entries)
        return Surface(
            **common,
            n_strikes=len(strike_data),
            implied_mean=_fmt(mean, 4),
            implied_cv=_fmt(cv, 6),
            implied_skew=_fmt(skew, 6),
            strikes=strike_data,
        )

    if surface_type == "density":
        entries_d = [
            (m.id, float(m.strike), float(m.strike_upper) if m.strike_upper else None,
             m.strike_direction, mp)
            for m, mp in valid if m.strike_direction
        ]
        if len(entries_d) < min_strikes:
            return None
        strike_data, mean, cv, skew = _compute_density(entries_d)
        return Surface(
            **common,
            n_strikes=len(strike_data),
            implied_mean=_fmt(mean, 4),
            implied_cv=_fmt(cv, 6),
            implied_skew=_fmt(skew, 6),
            strikes=strike_data,
        )

    if surface_type == "barrier":
        entries_b = [
            (m.id, float(m.strike), m.strike_direction, mp)
            for m, mp in valid if m.strike_direction
        ]
        if len(entries_b) < min_strikes:
            return None
        strike_data, peak, peak_cv, trough, trough_cv = _compute_barrier(entries_b)
        return Surface(
            **common,
            n_strikes=len(strike_data),
            implied_peak=_fmt(peak, 4),
            implied_peak_cv=_fmt(peak_cv, 6),
            implied_trough=_fmt(trough, 4),
            implied_trough_cv=_fmt(trough_cv, 6),
            strikes=strike_data,
        )

    return None
