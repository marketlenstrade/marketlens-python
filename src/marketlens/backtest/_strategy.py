from __future__ import annotations

from abc import ABC
from typing import Any

from marketlens.backtest._types import (
    Fill,
    Order,
    OrderSide,
    Position,
)
from marketlens.types.history import TradeEvent
from marketlens.types.market import Market
from marketlens.types.orderbook import OrderBook


class Strategy(ABC):
    """Override the hooks you need."""

    def on_book(self, ctx: StrategyContext, market: Market, book: OrderBook) -> None:
        """Called on every book state change (snapshot or delta)."""

    def on_trade(
        self, ctx: StrategyContext, market: Market, book: OrderBook, trade: TradeEvent,
    ) -> None:
        """Called on every historical trade. ``book`` = latest state at trade time."""

    def on_fill(self, ctx: StrategyContext, market: Market, fill: Fill) -> None:
        """Called when your order is filled."""

    def on_market_start(
        self, ctx: StrategyContext, market: Market, book: OrderBook,
    ) -> None:
        """Called once when a new market begins in the walk."""

    def on_market_end(self, ctx: StrategyContext, market: Market) -> None:
        """Called when a market's data is exhausted, before settlement."""



class StrategyContext:
    """Provided to strategy hooks for submitting orders and querying state."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    # ── Order submission ──────────────────────────────────────────

    def buy_yes(
        self,
        size: str,
        *,
        market_id: str | None = None,
        limit_price: str | None = None,
        cancel_after: int | None = None,
    ) -> Order:
        return self._engine.submit_order(
            OrderSide.BUY_YES, size,
            market_id=market_id, limit_price=limit_price, cancel_after=cancel_after,
        )

    def buy_no(
        self,
        size: str,
        *,
        market_id: str | None = None,
        limit_price: str | None = None,
        cancel_after: int | None = None,
    ) -> Order:
        return self._engine.submit_order(
            OrderSide.BUY_NO, size,
            market_id=market_id, limit_price=limit_price, cancel_after=cancel_after,
        )

    def sell_yes(
        self,
        size: str,
        *,
        market_id: str | None = None,
        limit_price: str | None = None,
        cancel_after: int | None = None,
    ) -> Order:
        return self._engine.submit_order(
            OrderSide.SELL_YES, size,
            market_id=market_id, limit_price=limit_price, cancel_after=cancel_after,
        )

    def sell_no(
        self,
        size: str,
        *,
        market_id: str | None = None,
        limit_price: str | None = None,
        cancel_after: int | None = None,
    ) -> Order:
        return self._engine.submit_order(
            OrderSide.SELL_NO, size,
            market_id=market_id, limit_price=limit_price, cancel_after=cancel_after,
        )

    # ── Order management ──────────────────────────────────────────

    def cancel(self, order: Order) -> None:
        self._engine.cancel_order(order)

    def cancel_all(self, *, market_id: str | None = None) -> None:
        self._engine.cancel_all_orders(market_id=market_id)

    # ── State queries ─────────────────────────────────────────────

    def position(self, market_id: str | None = None) -> Position:
        mid = market_id or self._engine.current_market.id
        return self._engine.portfolio.position(mid)

    @property
    def cash(self) -> str:
        return self._engine.portfolio.cash

    @property
    def equity(self) -> str:
        return self._engine.portfolio.equity

    @property
    def open_orders(self) -> list[Order]:
        return self._engine.open_orders

    @property
    def market(self) -> Market:
        return self._engine.current_market

    @property
    def book(self) -> OrderBook:
        return self._engine.current_book

    @property
    def time(self) -> int:
        return self._engine.current_time

    @property
    def books(self) -> dict[str, OrderBook]:
        return dict(self._engine._books)

    def reference_price(self, market_id: str | None = None) -> str | None:
        """Return the latest reference price for the market's underlying at the current time."""
        mid = market_id or self._engine.current_market.id
        underlying = self._engine._market_underlying.get(mid)
        return self._engine.get_reference_price(underlying, self._engine.current_time)

    # ── Backwards-compatible aliases ──────────────────────────────

    @property
    def current_market(self) -> Market:
        return self.market

    @property
    def current_book(self) -> OrderBook:
        return self.book

    @property
    def current_time(self) -> int:
        return self.time

    @property
    def event_books(self) -> dict[str, OrderBook]:
        return self.books
