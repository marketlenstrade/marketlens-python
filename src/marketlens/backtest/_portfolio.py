from __future__ import annotations

from marketlens.backtest._types import (
    Fill,
    OrderSide,
    Position,
    PositionSide,
    SettlementRecord,
)
from marketlens.types.market import Market
from marketlens.types.orderbook import OrderBook


class _MutablePosition:
    def __init__(self, market_id: str) -> None:
        self.market_id = market_id
        self.side = PositionSide.FLAT
        self.shares = 0.0
        self.avg_entry_price = 0.0
        self.cost_basis = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.total_fees = 0.0

    def add_shares(
        self, side: PositionSide, size: float, price: float, fee: float,
    ) -> float:
        """Add shares, returning the number netted against the opposite side.

        When buying the opposite side, matched YES+NO pairs are netted and
        the caller should credit ``matched * $1`` back to cash to simulate
        CTF merge.  Returns 0 when adding to the same side or from flat.
        """
        if self.side == PositionSide.FLAT:
            self.side = side
            self.shares = size
            self.avg_entry_price = price
            self.cost_basis = price * size
            self.total_fees += fee
            return 0.0
        elif self.side == side:
            total_cost = self.avg_entry_price * self.shares + price * size
            self.shares += size
            self.avg_entry_price = total_cost / self.shares
            self.cost_basis = self.avg_entry_price * self.shares
            self.total_fees += fee
            return 0.0
        else:
            # Opposite side — net down.  Matched YES+NO shares settle at $1
            # guaranteed, so buying the opposite side locks in settlement value.
            matched = min(self.shares, size)
            excess = size - matched

            settlement_value = 1.0 - price
            self.realized_pnl += (settlement_value - self.avg_entry_price) * matched

            self.shares -= matched
            if excess > 0.0:
                self.side = side
                self.shares = excess
                self.avg_entry_price = price
                self.cost_basis = excess * price
            elif self.shares == 0.0:
                self.side = PositionSide.FLAT
                self.avg_entry_price = 0.0
                self.cost_basis = 0.0
                self.unrealized_pnl = 0.0
            else:
                self.cost_basis = self.avg_entry_price * self.shares
            self.total_fees += fee
            return matched

    def remove_shares(self, size: float, price: float, fee: float) -> None:
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
            self.avg_entry_price = 0.0
            self.cost_basis = 0.0
            self.unrealized_pnl = 0.0

    def settle(self, settlement_price: float) -> float:
        if self.shares == 0:
            return 0.0
        pnl = (settlement_price - self.avg_entry_price) * self.shares
        self.realized_pnl += pnl
        self.shares = 0.0
        self.cost_basis = 0.0
        self.unrealized_pnl = 0.0
        self.side = PositionSide.FLAT
        self.avg_entry_price = 0.0
        return pnl

    def mark_to_market(self, current_price: float) -> None:
        if self.shares > 0:
            self.unrealized_pnl = (current_price - self.avg_entry_price) * self.shares
        else:
            self.unrealized_pnl = 0.0

    def snapshot(self) -> Position:
        return Position(
            market_id=self.market_id,
            side=self.side,
            shares=self.shares,
            avg_entry_price=self.avg_entry_price,
            cost_basis=self.cost_basis,
            unrealized_pnl=self.unrealized_pnl,
            realized_pnl=self.realized_pnl,
            total_fees=self.total_fees,
        )


class Portfolio:
    def __init__(self, initial_cash: float = 10_000.0) -> None:
        self._initial_cash = float(initial_cash)
        self._cash = self._initial_cash
        self._positions: dict[str, _MutablePosition] = {}
        self._total_fees = 0.0

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def initial_cash(self) -> float:
        return self._initial_cash

    @property
    def total_fees(self) -> float:
        return self._total_fees

    @property
    def equity(self) -> float:
        total = self._cash
        for pos in self._positions.values():
            total += pos.cost_basis + pos.unrealized_pnl
        return total

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
        price = fill.price
        size = fill.size
        fee = fill.fee

        if fill.side in (OrderSide.BUY_YES, OrderSide.BUY_NO):
            target_side = (
                PositionSide.YES if fill.side == OrderSide.BUY_YES else PositionSide.NO
            )
            matched = pos.add_shares(target_side, size, price, fee)
            self._cash -= price * size + fee
            # Simulate CTF merge: matched YES+NO pairs return $1 each.
            if matched > 0.0:
                self._cash += matched
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
            settlement_price = 1.0 if market.winning_outcome_index == 0 else 0.0
        else:
            settlement_price = 1.0 if market.winning_outcome_index == 1 else 0.0

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
            shares=pre_shares,
            avg_entry_price=pre_entry,
            settlement_price=settlement_price,
            pnl=(settlement_price - pre_entry) * pre_shares,
            fees=pre_fees,
            winning_outcome=market.winning_outcome,
            resolved_at=timestamp,
        )

    def mark_to_market(self, market_id: str, book: OrderBook) -> None:
        pos = self._get_or_create(market_id)
        # `best_bid`/`best_ask` default to 0.5 when the side is empty —
        # use the level count to detect a real top-of-book.
        if pos.side == PositionSide.YES and book.bid_levels:
            pos.mark_to_market(book.best_bid)
        elif pos.side == PositionSide.NO and book.ask_levels:
            pos.mark_to_market(1.0 - book.best_ask)
        else:
            pos.mark_to_market(pos.avg_entry_price)

    def can_sell(self, market_id: str, side: OrderSide, size: float) -> bool:
        pos = self._get_or_create(market_id)
        if side == OrderSide.SELL_YES and pos.side == PositionSide.YES:
            return pos.shares >= size
        if side == OrderSide.SELL_NO and pos.side == PositionSide.NO:
            return pos.shares >= size
        return False
