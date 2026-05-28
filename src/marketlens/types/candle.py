from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from marketlens.types._validators import none_to_zero


class Candle(BaseModel):
    model_config = ConfigDict(frozen=True)

    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    vwap: float = 0.0
    volume: float
    trade_count: int

    _coerce = none_to_zero("vwap")
