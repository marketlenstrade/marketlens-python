from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, ConfigDict

from marketlens.types.orderbook import PriceLevel


class SnapshotEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["snapshot"] = "snapshot"
    t: int
    is_reseed: bool
    bids: list[PriceLevel]
    asks: list[PriceLevel]


class DeltaEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["delta"] = "delta"
    t: int
    price: float
    size: float
    side: str


class TradeEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["trade"] = "trade"
    t: int
    id: str
    price: float
    size: float
    side: str


HistoryEvent = Union[SnapshotEvent, DeltaEvent, TradeEvent]
