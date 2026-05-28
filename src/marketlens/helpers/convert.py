"""DataFrame conversion with sensible index and timestamp handling.

Turns epoch-ms timestamps into ``datetime64[ns, UTC]`` and sets a natural
time-based index. Numeric fields are already ``float`` on the model so no
extra coercion is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pandas as pd
from pydantic import BaseModel


@dataclass(frozen=True)
class _DFConfig:
    """Declares how to shape a model's DataFrame output."""
    timestamps: tuple[str, ...] = ()
    index: str | None = None
    exclude: tuple[str, ...] = ()


_REGISTRY: dict[type, _DFConfig] | None = None


def _get_registry() -> dict[type, _DFConfig]:
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY

    from marketlens.types.candle import Candle
    from marketlens.types.event import Event
    from marketlens.types.market import Market
    from marketlens.types.orderbook import BookMetrics
    from marketlens.types.series import Series
    from marketlens.types.signal import Surface
    from marketlens.types.trade import Trade

    _REGISTRY = {
        Candle: _DFConfig(
            timestamps=("open_time", "close_time"),
            index="open_time",
        ),
        Trade: _DFConfig(
            timestamps=("platform_timestamp", "collected_at"),
            index="platform_timestamp",
        ),
        BookMetrics: _DFConfig(
            timestamps=("t",),
            index="t",
        ),
        Market: _DFConfig(
            timestamps=("open_time", "close_time", "resolved_at", "platform_resolved_at", "created_at", "updated_at"),
            exclude=("outcomes",),
        ),
        Event: _DFConfig(
            timestamps=("start_date", "end_date", "created_at", "updated_at"),
        ),
        Series: _DFConfig(
            timestamps=("first_market_close", "last_market_close"),
        ),
        Surface: _DFConfig(
            timestamps=("computed_at", "expiry_ms"),
            index="computed_at",
            exclude=("strikes",),
        ),
    }
    return _REGISTRY


def models_to_dataframe(items: Sequence[BaseModel], model_cls: type | None = None) -> pd.DataFrame:
    """Convert a sequence of Pydantic models to a typed DataFrame.

    Numeric fields are native ``float`` on the model and round-trip directly
    into ``float64`` columns. Epoch-ms timestamps become
    ``datetime64[ns, UTC]`` and a natural index is set when one exists.
    """

    if not items:
        return pd.DataFrame()

    model_cls = model_cls or type(items[0])
    registry = _get_registry()
    config = registry.get(model_cls)

    rows = []
    for item in items:
        d = item.model_dump()
        if config:
            for k in config.exclude:
                d.pop(k, None)
        rows.append(d)

    df = pd.DataFrame(rows)

    if config is None:
        return df

    for col in config.timestamps:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], unit="ms", utc=True, errors="coerce")

    if config.index and config.index in df.columns:
        df = df.set_index(config.index)

    return df
