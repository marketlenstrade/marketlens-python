from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from marketlens.types._validators import none_to_zero


class ReferenceCandle(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    _coerce = none_to_zero("volume")


class ReferenceTrade(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: int
    price: float
    quantity: float
    is_buyer_maker: bool
