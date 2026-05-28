from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class OrderSide(str, Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    SELL_YES = "SELL_YES"
    SELL_NO = "SELL_NO"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class PositionSide(str, Enum):
    YES = "YES"
    NO = "NO"
    FLAT = "FLAT"


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    market_id: str
    side: OrderSide
    price: float
    size: float
    fee: float
    timestamp: int
    is_maker: bool


class Order(BaseModel):
    id: str
    market_id: str
    side: OrderSide
    order_type: OrderType
    size: float
    limit_price: float | None = None
    submitted_at: int
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    avg_fill_price: float | None = None
    total_fees: float = 0.0
    fills: list[Fill] = Field(default_factory=list)
    cancel_after: int | None = None


class Position(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_id: str
    side: PositionSide
    shares: float
    avg_entry_price: float
    cost_basis: float
    unrealized_pnl: float
    realized_pnl: float
    total_fees: float


class SettlementRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_id: str
    series_id: str | None = None
    side: PositionSide
    shares: float
    avg_entry_price: float
    settlement_price: float
    pnl: float
    fees: float
    winning_outcome: str | None
    resolved_at: int
