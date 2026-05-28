from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from marketlens.types._validators import none_to_half, none_to_zero


class Outcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    index: int
    platform_token_id: str
    last_price: float = 0.5

    _coerce = none_to_half("last_price")


class Market(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    platform: str
    platform_market_id: str
    event_id: str
    event_title: str | None = None
    category: str | None = None
    series_id: str | None = None
    series_title: str | None = None
    series_recurrence: str | None = None
    question: str
    market_type: str
    status: str
    outcomes: list[Outcome]
    winning_outcome: str | None = None
    winning_outcome_index: int | None = None
    tick_size: float
    volume: float = 0.0
    liquidity: float = 0.0
    open_time: int | None = None
    close_time: int | None = None
    resolved_at: int | None = None
    platform_resolved_at: int | None = None
    strike: float | None = None
    strike_upper: float | None = None
    strike_direction: str | None = None
    underlying: str | None = None
    created_at: int
    updated_at: int

    _coerce = none_to_zero("volume", "liquidity")
