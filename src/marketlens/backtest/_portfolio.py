from __future__ import annotations

from typing import Literal

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
    """One-sided position for a single token (YES or NO). Side is fixed at creation."""

    def __init__(
        self,
        market_id: str,
        side: Literal[PositionSide.YES, PositionSide.NO],
    ) -> None:
        self.market_id = market_id
        self._side: Literal[PositionSide.YES, PositionSide.NO] = side
        self.shares = 0.0
        self.avg_entry_price = 0.0
        self.cost_basis = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.total_fees = 0.0

    def add_shares(
        self,
        side: Literal[PositionSide.YES, PositionSide.NO],
        size: float,
        price: float,
        fee: float,
    ) -> None:
        if side != self._side:
            raise ValueError(f"Expected side {self._side!r}, got {side!r}")

        total_cost = self.avg_entry_price * self.shares + price * size
        self.shares += size
        self.avg_entry_price = total_cost / self.shares
        self.cost_basis = self.avg_entry_price * self.shares
        self.total_fees += fee

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
            side=self._side if self.shares > 0 else PositionSide.FLAT,
            shares=self.shares,
            avg_entry_price=self.avg_entry_price,
            cost_basis=self.cost_basis,
            unrealized_pnl=self.unrealized_pnl,
            realized_pnl=self.realized_pnl,
            total_fees=self.total_fees,
        )


class _MarketMutablePosition:
    """Aggregates separate YES and NO token positions for a single market."""

    def __init__(self, market_id: str) -> None:
        self.market_id = market_id
        self._yes_position = _MutablePosition(market_id, PositionSide.YES)
        self._no_position = _MutablePosition(market_id, PositionSide.NO)

    @property
    def yes_position(self) -> Position:
        return self._yes_position.snapshot()

    @property
    def no_position(self) -> Position:
        return self._no_position.snapshot()

    @property
    def side(self) -> PositionSide:
        # returns NET position side
        y = self._yes_position.shares
        n = self._no_position.shares
        if y > n:
            return PositionSide.YES
        elif n > y:
            return PositionSide.NO
        else:
            return PositionSide.FLAT

    def add_shares(
        self,
        side: Literal[PositionSide.YES, PositionSide.NO],
        size: float,
        price: float,
        fee: float,
    ) -> None:
        if side == PositionSide.YES:
            self._yes_position.add_shares(side, size, price, fee)
        else:
            self._no_position.add_shares(side, size, price, fee)

    def remove_shares(
        self,
        side: Literal[PositionSide.YES, PositionSide.NO],
        size: float,
        price: float,
        fee: float,
    ) -> None:
        if side == PositionSide.YES:
            self._yes_position.remove_shares(size, price, fee)
        else:
            self._no_position.remove_shares(size, price, fee)

    def split(self, size: float) -> None:
        self.add_shares(PositionSide.YES, size, 0.5, 0.0)
        self.add_shares(PositionSide.NO, size, 0.5, 0.0)

    def merge(self, size: float) -> None:
        self.remove_shares(PositionSide.YES, size, 0.5, 0.0)
        self.remove_shares(PositionSide.NO, size, 0.5, 0.0)

    def settle(self, yes_price: float, no_price: float) -> float:
        return self._yes_position.settle(yes_price) + self._no_position.settle(no_price)

    def mark_to_market(self, book: OrderBook) -> None:
        yes_price = book.best_bid if book.bid_levels else self._yes_position.avg_entry_price
        no_price = (1.0 - book.best_ask) if book.ask_levels else self._no_position.avg_entry_price
        self._yes_position.mark_to_market(yes_price)
        self._no_position.mark_to_market(no_price)

    def snapshot(self) -> Position:
        y = self._yes_position
        n = self._no_position

        net_shares = y.shares - n.shares
        total_cost = y.cost_basis + n.cost_basis
        paired = min(y.shares, n.shares)
        # Each YES+NO pair is worth a guaranteed $1, so credit $1/pair against
        # cost. Trade-off: if the pairs were bought cheap (YES+NO < $1), the
        # credit can exceed total_cost -> net_cost is negative and so is
        # avg_entry_price. It's a value-adjusted basis, not a real entry price.
        net_cost = total_cost - paired
        avg_price = net_cost / abs(net_shares) if net_shares != 0 else 0.0

        return Position(
            market_id=self.market_id,
            side=self.side,
            shares=abs(net_shares),     # directional position size
            # Trade-off: cost_basis is gross (both legs) while shares is net, so
            # cost_basis != avg_entry_price * shares. Intentional — equity needs
            # the gross cost to mark both legs correctly.
            avg_entry_price=avg_price,
            cost_basis=total_cost,
            unrealized_pnl=y.unrealized_pnl + n.unrealized_pnl,
            realized_pnl=y.realized_pnl + n.realized_pnl,
            total_fees=y.total_fees + n.total_fees,
        )


class Portfolio:
    def __init__(self, initial_cash: float = 10_000.0) -> None:
        self._initial_cash = float(initial_cash)
        self._cash = self._initial_cash
        self._positions: dict[str, _MarketMutablePosition] = {}
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
            snap = pos.snapshot()
            total += snap.cost_basis + snap.unrealized_pnl
        return total

    def _get_or_create(self, market_id: str) -> _MarketMutablePosition:
        if market_id not in self._positions:
            self._positions[market_id] = _MarketMutablePosition(market_id)
        return self._positions[market_id]

    def position(self, market_id: str) -> Position:
        return self._get_or_create(market_id).snapshot()

    def yes_position(self, market_id: str) -> Position:
        return self._get_or_create(market_id).yes_position

    def no_position(self, market_id: str) -> Position:
        return self._get_or_create(market_id).no_position

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
            pos.add_shares(target_side, size, price, fee)
            self._cash -= price * size + fee
        else:  # SELL_YES, SELL_NO
            sell_side = (
                PositionSide.YES if fill.side == OrderSide.SELL_YES else PositionSide.NO
            )
            pos.remove_shares(sell_side, size, price, fee)
            self._cash += price * size - fee

        self._total_fees += fee

    def split(self, market_id: str, size: float) -> None:
        self._get_or_create(market_id).split(size)
        self._cash -= size

    def merge(self, market_id: str, size: float) -> None:
        self._get_or_create(market_id).merge(size)
        self._cash += size

    def settle_market(
        self,
        market: Market,
        timestamp: int,
        *,
        series_id: str | None = None,
    ) -> SettlementRecord | None:
        pos = self._get_or_create(market.id)
        # Only skip when nothing is held. A fully hedged position
        # has side==FLAT yet must still settle.
        if pos.yes_position.side == PositionSide.FLAT and pos.no_position.side == PositionSide.FLAT:
            return None
        if market.winning_outcome_index is None:
            return None

        yes_price = 1.0 if market.winning_outcome_index == 0 else 0.0
        no_price = 1.0 if market.winning_outcome_index == 1 else 0.0

        # Trade-off: a two-leg (hedged) position is collapsed into one record.
        # `pnl`/cash stay exact, but side/shares/price are the *net* view and
        # won't reconcile as (settlement_price - avg)*shares when both legs held.
        position_snapshot = self.position(market.id)
        price = yes_price if position_snapshot.side == PositionSide.YES else no_price
        pnl = pos.settle(yes_price, no_price)
        self._cash += position_snapshot.cost_basis + pnl

        return SettlementRecord( 
                market_id=market.id,
                series_id=series_id,
                side=position_snapshot.side,
                shares=position_snapshot.shares,
                avg_entry_price=position_snapshot.avg_entry_price,
                settlement_price=price,
                pnl=pnl,
                fees=position_snapshot.total_fees,
                winning_outcome=market.winning_outcome,
                resolved_at=timestamp,
            )


    def mark_to_market(self, market_id: str, book: OrderBook) -> None:
        self._get_or_create(market_id).mark_to_market(book)

    def can_sell(self, market_id: str, side: OrderSide, size: float) -> bool:
        pos = self._get_or_create(market_id)
        if side == OrderSide.SELL_YES:
            return pos._yes_position.shares >= size
        if side == OrderSide.SELL_NO:
            return pos._no_position.shares >= size
        return False
