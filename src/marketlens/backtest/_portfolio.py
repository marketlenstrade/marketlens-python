from __future__ import annotations

from decimal import Decimal

from marketlens.backtest._types import (
    Fill,
    OrderSide,
    Position,
    PositionSide,
    SettlementRecord,
)
from marketlens.types.market import Market
from marketlens.types.orderbook import OrderBook

_FOUR = Decimal("0.0001")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class _MutablePosition:
    def __init__(self, market_id: str) -> None:
        self.market_id = market_id
        self.side = PositionSide.FLAT
        self.shares = _ZERO
        self.avg_entry_price = _ZERO
        self.cost_basis = _ZERO
        self.unrealized_pnl = _ZERO
        self.realized_pnl = _ZERO
        self.total_fees = _ZERO

    def add_shares(
        self, side: PositionSide, size: Decimal, price: Decimal, fee: Decimal,
    ) -> None:
        if self.side == PositionSide.FLAT:
            self.side = side
            self.shares = size
            self.avg_entry_price = price
            self.cost_basis = price * size
        elif self.side == side:
            total_cost = self.avg_entry_price * self.shares + price * size
            self.shares += size
            self.avg_entry_price = total_cost / self.shares
            self.cost_basis = self.avg_entry_price * self.shares
        else:
            # Opposite side — net down.  Matched YES+NO shares settle at $1
            # guaranteed, so buying the opposite side locks in settlement value.
            matched = min(self.shares, size)
            excess = size - matched

            settlement_value = _ONE - price
            self.realized_pnl += (settlement_value - self.avg_entry_price) * matched

            self.shares -= matched
            if excess > _ZERO:
                self.side = side
                self.shares = excess
                self.avg_entry_price = price
                self.cost_basis = excess * price
            elif self.shares == _ZERO:
                self.side = PositionSide.FLAT
                self.avg_entry_price = _ZERO
                self.cost_basis = _ZERO
                self.unrealized_pnl = _ZERO
            else:
                self.cost_basis = self.avg_entry_price * self.shares
        self.total_fees += fee

    def remove_shares(self, size: Decimal, price: Decimal, fee: Decimal) -> None:
        if self.shares < size:
            raise ValueError(
                f"Cannot sell {size} shares: only holding {self.shares}"
            )
        pnl = (price - self.avg_entry_price) * size
        self.realized_pnl += pnl
        self.shares -= size
        self.cost_basis = self.avg_entry_price * self.shares
        self.total_fees += fee
        if self.shares == 0:
            self.side = PositionSide.FLAT
            self.avg_entry_price = _ZERO
            self.cost_basis = _ZERO
            self.unrealized_pnl = _ZERO

    def settle(self, settlement_price: Decimal) -> Decimal:
        if self.shares == 0:
            return _ZERO
        pnl = (settlement_price - self.avg_entry_price) * self.shares
        self.realized_pnl += pnl
        self.shares = _ZERO
        self.cost_basis = _ZERO
        self.unrealized_pnl = _ZERO
        self.side = PositionSide.FLAT
        self.avg_entry_price = _ZERO
        return pnl

    def mark_to_market(self, current_price: Decimal) -> None:
        if self.shares > 0:
            self.unrealized_pnl = (current_price - self.avg_entry_price) * self.shares
        else:
            self.unrealized_pnl = _ZERO

    def snapshot(self) -> Position:
        return Position(
            market_id=self.market_id,
            side=self.side,
            shares=str(self.shares.quantize(_FOUR)),
            avg_entry_price=str(self.avg_entry_price.quantize(_FOUR)),
            cost_basis=str(self.cost_basis.quantize(_FOUR)),
            unrealized_pnl=str(self.unrealized_pnl.quantize(_FOUR)),
            realized_pnl=str(self.realized_pnl.quantize(_FOUR)),
            total_fees=str(self.total_fees.quantize(_FOUR)),
        )


class Portfolio:
    def __init__(self, initial_cash: str = "10000.0000") -> None:
        self._initial_cash = Decimal(initial_cash)
        self._cash = self._initial_cash
        self._positions: dict[str, _MutablePosition] = {}
        self._total_fees = _ZERO

    @property
    def cash(self) -> str:
        return str(self._cash.quantize(_FOUR))

    @property
    def initial_cash(self) -> str:
        return str(self._initial_cash.quantize(_FOUR))

    @property
    def total_fees(self) -> str:
        return str(self._total_fees.quantize(_FOUR))

    @property
    def equity(self) -> str:
        total = self._cash
        for pos in self._positions.values():
            total += pos.cost_basis + pos.unrealized_pnl
        return str(total.quantize(_FOUR))

    def _get_or_create(self, market_id: str) -> _MutablePosition:
        if market_id not in self._positions:
            self._positions[market_id] = _MutablePosition(market_id)
        return self._positions[market_id]

    def position(self, market_id: str) -> Position:
        return self._get_or_create(market_id).snapshot()

    def positions(self) -> list[Position]:
        return [pos.snapshot() for pos in self._positions.values()]

    def apply_fill(self, fill: Fill) -> None:
        pos = self._get_or_create(fill.market_id)
        price = Decimal(fill.price)
        size = Decimal(fill.size)
        fee = Decimal(fill.fee)

        if fill.side in (OrderSide.BUY_YES, OrderSide.BUY_NO):
            target_side = (
                PositionSide.YES if fill.side == OrderSide.BUY_YES else PositionSide.NO
            )
            pos.add_shares(target_side, size, price, fee)
            self._cash -= price * size + fee
        else:  # SELL_YES, SELL_NO
            pos.remove_shares(size, price, fee)
            self._cash += price * size - fee

        self._total_fees += fee

    def settle_market(self, market: Market, timestamp: int, *, series_id: str | None = None) -> SettlementRecord | None:
        pos = self._get_or_create(market.id)
        if pos.side == PositionSide.FLAT:
            return None
        if market.winning_outcome_index is None:
            return None

        if pos.side == PositionSide.YES:
            settlement_price = _ONE if market.winning_outcome_index == 0 else _ZERO
        else:
            settlement_price = _ONE if market.winning_outcome_index == 1 else _ZERO

        pre_shares = pos.shares
        pre_entry = pos.avg_entry_price
        pre_side = pos.side
        pre_fees = pos.total_fees

        pos.settle(settlement_price)
        self._cash += settlement_price * pre_shares

        return SettlementRecord(
            market_id=market.id,
            series_id=series_id,
            side=pre_side,
            shares=str(pre_shares.quantize(_FOUR)),
            avg_entry_price=str(pre_entry.quantize(_FOUR)),
            settlement_price=str(settlement_price.quantize(_FOUR)),
            pnl=str(((settlement_price - pre_entry) * pre_shares).quantize(_FOUR)),
            fees=str(pre_fees.quantize(_FOUR)),
            winning_outcome=market.winning_outcome,
            resolved_at=timestamp,
        )

    def mark_to_market(self, market_id: str, book: OrderBook) -> None:
        pos = self._get_or_create(market_id)
        if pos.side == PositionSide.YES and book.best_bid:
            pos.mark_to_market(Decimal(book.best_bid))
        elif pos.side == PositionSide.NO and book.best_ask:
            pos.mark_to_market(_ONE - Decimal(book.best_ask))
        else:
            pos.mark_to_market(pos.avg_entry_price)

    def can_sell(self, market_id: str, side: OrderSide, size: Decimal) -> bool:
        pos = self._get_or_create(market_id)
        if side == OrderSide.SELL_YES and pos.side == PositionSide.YES:
            return pos.shares >= size
        if side == OrderSide.SELL_NO and pos.side == PositionSide.NO:
            return pos.shares >= size
        return False
