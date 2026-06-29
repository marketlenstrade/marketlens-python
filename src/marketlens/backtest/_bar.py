"""Bar data path for the signal-level (alpha) backtest.

One ``Bar`` per market per ``resolution``, from ``orderbook.metrics`` (the mid,
``price="mid"``) or ``markets.candles`` (the close, ``price="close"``), plus the
fill model and the offline parquet cache. Far fewer events than the L2 firehose,
so long windows stay cheap.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pyarrow.parquet as pq

from marketlens.backtest._fees import FeeModel
from marketlens.backtest._types import Fill, OrderSide

_DAY_MS = 86_400_000

# Max span the metrics endpoint serves per request, by resolution — mirrors
# ``_MAX_RANGE_MS`` in api/routes/orderbook.py. A wider request is rejected, not
# paginated, so the stream slices ``[after, before)`` into chunks this wide and
# stitches them. Each value is a multiple of its resolution, so chunks abut on a
# bucket boundary (``after`` inclusive, ``before`` exclusive: no gap, no overlap).
_METRICS_MAX_RANGE_MS = {
    "1m": 1 * _DAY_MS, "5m": 7 * _DAY_MS, "15m": 14 * _DAY_MS,
    "1h": 30 * _DAY_MS, "4h": 90 * _DAY_MS, "1d": 365 * _DAY_MS,
}
_METRICS_RESOLUTIONS = frozenset(_METRICS_MAX_RANGE_MS)
# Candles support finer buckets and have no per-request span cap.
_CANDLE_RESOLUTIONS = frozenset(
    {"1s", "5s", "10s", "30s", "1m", "5m", "15m", "1h", "4h", "1d"}
)
# Bar length in ms, used to annualize time-series risk metrics.
_RESOLUTION_MS = {
    "1s": 1_000, "5s": 5_000, "10s": 10_000, "30s": 30_000,
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}

_PRICE_DP = 4
_SHARE_DP = 4


@dataclass(frozen=True)
class Bar:
    """One time bucket of market state at the backtest resolution.

    ``mid`` is the price the engine fills and marks at: the order-book midpoint
    (``price="mid"``) or the last-trade close (``price="close"``). OHLCV is set
    only for candles; spread and depth only for metrics.
    """

    t: int
    mid: float
    spread: float = 0.0
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None


@dataclass
class AlphaConfig:
    """Signal-level backtest config: the shared knobs (cash, fees, slippage) plus
    the bar-cadence ones. Microstructure knobs do not apply and are absent."""

    initial_cash: float = 10_000.0
    resolution: str = "1m"          # bar cadence
    price: str = "mid"              # "mid" (metrics) | "close" (candles)
    fill: str = "next"              # "next" bar (no look-ahead) | "close" (same bar)
    slippage_bps: int = 0
    fee_model: FeeModel | None = None
    fees: str | None = "polymarket"
    progress: bool = True
    download_concurrency: int = 8

    def validate(self) -> None:
        if self.price not in ("mid", "close"):
            raise ValueError(f"price must be 'mid' or 'close', got {self.price!r}.")
        if self.fill not in ("next", "close"):
            raise ValueError(f"fill must be 'next' or 'close', got {self.fill!r}.")
        allowed = _METRICS_RESOLUTIONS if self.price == "mid" else _CANDLE_RESOLUTIONS
        if self.resolution not in allowed:
            raise ValueError(
                f"resolution {self.resolution!r} is invalid for price={self.price!r}; "
                f"use one of {', '.join(sorted(allowed, key=_RESOLUTION_MS.__getitem__))}."
            )


class BarFillModel:
    """Fills a target delta at the bar mid plus fixed slippage, charged the fee
    model as a taker. The one cost term that stands in for all microstructure."""

    def __init__(self, fee_model: FeeModel, *, slippage_bps: int = 0) -> None:
        self._fee_model = fee_model
        self._slippage_bps = float(slippage_bps)

    def make_fill(self, order_id: str, market_id: str, side: OrderSide,
                  size: float, mid: float, timestamp: int) -> Fill:
        # The engine only ever BUYs (reductions/flips go through BUY_NO + CTF
        # merge), so slippage always moves the price worse for a buy.
        price = mid if side == OrderSide.BUY_YES else 1.0 - mid
        if self._slippage_bps:
            price += price * self._slippage_bps / 10_000.0
            price = max(0.0, min(1.0, price))
        fee = self._fee_model.calculate(price, size, is_maker=False)
        return Fill(
            order_id=order_id, market_id=market_id, side=side,
            price=round(price, _PRICE_DP), size=round(size, _SHARE_DP),
            fee=round(fee, _SHARE_DP), timestamp=timestamp, is_maker=False,
        )


# ── Bar streams ───────────────────────────────────────────────────────────


def iter_bars(
    orderbook: Any, markets: Any, market_id: str, after_ms: int, before_ms: int,
    *, resolution: str, price: str,
) -> Iterator[Bar]:
    """Yield bars for one market in ``[after_ms, before_ms)`` from the API.

    ``price="mid"`` chunks the window under the metrics span cap; ``price="close"``
    pages candles freely. Takes the ``orderbook`` and ``markets`` resources so
    both the engine and the exports resource can call it.
    """
    if after_ms >= before_ms:
        return
    if price == "mid":
        max_span = _METRICS_MAX_RANGE_MS[resolution]
        w0 = after_ms
        while w0 < before_ms:
            w1 = min(w0 + max_span, before_ms)
            for m in orderbook.metrics(market_id, after=w0, before=w1, resolution=resolution):
                yield Bar(t=m.t, mid=m.midpoint, spread=m.spread,
                          bid_depth=m.bid_depth, ask_depth=m.ask_depth)
            w0 = w1
    else:
        for c in markets.candles(market_id, resolution=resolution, after=after_ms, before=before_ms):
            yield Bar(t=c.close_time, mid=c.close, open=c.open, high=c.high,
                      low=c.low, close=c.close, volume=c.volume)


# ── Offline cache (parquet, columnar via pyarrow) ──────────────────────────

def bar_file(data_dir: str | Path, market_id: str, resolution: str, price: str) -> Path:
    kind = "metrics" if price == "mid" else "candles"
    return Path(data_dir) / f"{kind}-{market_id}-{resolution}.parquet"


def _opt(v: Any) -> float | None:
    return float(v) if v is not None else None


def iter_bars_parquet(
    path: Path, *, price: str, after_ms: int | None = None, before_ms: int | None = None,
) -> Iterator[Bar]:
    """Read a server-exported parquet (the metrics or candles schema) into Bars.

    The file is produced by ``client.exports.download_market_bars`` from the
    ``/orderbook/metrics/export`` or ``/candles/export`` endpoint, so its columns
    are the API's own, not a bespoke bar schema.
    """
    tbl = pq.read_table(path)
    if price == "mid":
        t = tbl.column("t").to_pylist()
        mid = tbl.column("midpoint").to_pylist()
        spread = tbl.column("spread").to_pylist()
        bd = tbl.column("bid_depth").to_pylist()
        ad = tbl.column("ask_depth").to_pylist()
        for i in range(len(t)):
            ti = int(t[i])
            if after_ms is not None and ti < after_ms:
                continue
            if before_ms is not None and ti >= before_ms:
                break
            if mid[i] is None:        # no book in this bucket -> unpriceable
                continue
            yield Bar(t=ti, mid=float(mid[i]), spread=float(spread[i] or 0.0),
                      bid_depth=float(bd[i] or 0.0), ask_depth=float(ad[i] or 0.0))
    else:
        ct = tbl.column("close_time").to_pylist()
        op = tbl.column("open").to_pylist()
        hi = tbl.column("high").to_pylist()
        lo = tbl.column("low").to_pylist()
        cl = tbl.column("close").to_pylist()
        vol = tbl.column("volume").to_pylist()
        for i in range(len(ct)):
            ti = int(ct[i])
            if after_ms is not None and ti < after_ms:
                continue
            if before_ms is not None and ti >= before_ms:
                break
            if cl[i] is None:
                continue
            yield Bar(t=ti, mid=float(cl[i]), open=_opt(op[i]), high=_opt(hi[i]),
                      low=_opt(lo[i]), close=_opt(cl[i]), volume=_opt(vol[i]))
