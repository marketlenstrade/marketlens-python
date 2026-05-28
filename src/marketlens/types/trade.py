from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from marketlens.types._validators import none_to_zero


class Trade(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    market_id: str
    platform: str
    price: float
    size: float
    side: str
    platform_timestamp: int
    collected_at: int
    fee_rate_bps: float = 0.0

    _coerce = none_to_zero("fee_rate_bps")
