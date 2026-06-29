from __future__ import annotations

import bisect
from abc import ABC
from typing import TYPE_CHECKING, Any

from marketlens.backtest._types import (
    Fill,
    Order,
    OrderSide,
    Position,
)
from marketlens.types.history import TradeEvent
from marketlens.types.market import Market
from marketlens.types.orderbook import OrderBook

if TYPE_CHECKING:
    from marketlens.backtest._bar import Bar


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

    def on_reject(self, ctx: StrategyContext, market: Market, order: Order) -> None:
        """Called when your order is rejected or fails to submit."""

    def on_market_start(
        self, ctx: StrategyContext, market: Market, book: OrderBook,
    ) -> None:
        """Called once when a new market begins in the walk."""

    def on_market_end(self, ctx: StrategyContext, market: Market) -> None:
        """Called when a market's data is exhausted, before settlement."""


class _ContextBase:
    """State queries shared by every backtest mode.

    Both the tick-level :class:`StrategyContext` and the bar-level
    :class:`AlphaContext` read position, cash, equity, time, and reference
    prices the same way; only how you *act* (place orders vs set targets)
    differs, so the verbs live on the subclasses.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    # ── State queries ─────────────────────────────────────────────

    def position(self, market_id: str | None = None) -> Position:
        """Net position for the market (YES and NO legs combined)."""
        mid = market_id or self._engine.current_market.id
        return self._engine.portfolio.position(mid)

    def yes_position(self, market_id: str | None = None) -> Position:
        """The YES leg on its own. Useful with ``auto_merge=False``, where the
        YES and NO legs are held separately; ``position()`` returns their net."""
        mid = market_id or self._engine.current_market.id
        return self._engine.portfolio.yes_position(mid)

    def no_position(self, market_id: str | None = None) -> Position:
        """The NO leg on its own. See :meth:`yes_position`."""
        mid = market_id or self._engine.current_market.id
        return self._engine.portfolio.no_position(mid)

    @property
    def cash(self) -> float:
        return self._engine.portfolio.cash

    @property
    def equity(self) -> float:
        return self._engine.portfolio.equity

    @property
    def market(self) -> Market:
        return self._engine.current_market

    @property
    def time(self) -> int:
        return self._engine.current_time

    def reference_price(self, market_id: str | None = None) -> float | None:
        """Return the latest reference price for the market's underlying at the current time."""
        mid = market_id or self._engine.current_market.id
        underlying = self._engine._market_underlying.get(mid)
        return self._engine.get_reference_price(underlying, self._engine.current_time)

    def reference_prices(self, market_id: str | None = None) -> list[tuple[int, float]]:
        """Return the full reference price history as ``[(timestamp_ms, price), ...]``.

        Only includes prices up to the current backtest time.
        """
        mid = market_id or self._engine.current_market.id
        underlying = self._engine._market_underlying.get(mid)
        if underlying is None or underlying not in self._engine._ref_prices:
            return []
        all_prices = self._engine._ref_prices[underlying]
        # Only return prices up to current time to prevent lookahead.
        end = bisect.bisect_right(all_prices, (self._engine.current_time, float("inf")))
        return all_prices[:end]

    def log_signal(self, **metadata: Any) -> None:
        """No-op; lets a strategy record signal metadata without branching."""

    # ── Backwards-compatible aliases ──────────────────────────────

    @property
    def current_market(self) -> Market:
        return self.market

    @property
    def current_time(self) -> int:
        return self.time


class StrategyContext(_ContextBase):
    """Provided to tick-level strategy hooks for submitting orders and querying state."""

    # ── Order submission ──────────────────────────────────────────

    def buy_yes(
        self,
        size: float | int | str,
        *,
        market_id: str | None = None,
        limit_price: float | int | str | None = None,
        cancel_after: int | None = None,
    ) -> Order:
        return self._engine.submit_order(
            OrderSide.BUY_YES, size,
            market_id=market_id, limit_price=limit_price, cancel_after=cancel_after,
        )

    def buy_no(
        self,
        size: float | int | str,
        *,
        market_id: str | None = None,
        limit_price: float | int | str | None = None,
        cancel_after: int | None = None,
    ) -> Order:
        return self._engine.submit_order(
            OrderSide.BUY_NO, size,
            market_id=market_id, limit_price=limit_price, cancel_after=cancel_after,
        )

    def sell_yes(
        self,
        size: float | int | str,
        *,
        market_id: str | None = None,
        limit_price: float | int | str | None = None,
        cancel_after: int | None = None,
    ) -> Order:
        return self._engine.submit_order(
            OrderSide.SELL_YES, size,
            market_id=market_id, limit_price=limit_price, cancel_after=cancel_after,
        )

    def sell_no(
        self,
        size: float | int | str,
        *,
        market_id: str | None = None,
        limit_price: float | int | str | None = None,
        cancel_after: int | None = None,
    ) -> Order:
        return self._engine.submit_order(
            OrderSide.SELL_NO, size,
            market_id=market_id, limit_price=limit_price, cancel_after=cancel_after,
        )

    def buy_batch(
        self,
        orders: list[tuple[str, str, str]],
        *,
        market_id: str | None = None,
    ) -> list[Order]:
        """Submit multiple limit buy orders.

        Args:
            orders: List of ``(side, size, limit_price)`` tuples where
                    *side* is ``"YES"`` or ``"NO"``.
            market_id: Override market (defaults to current).

        In backtest, each order is submitted individually.
        """
        results: list[Order] = []
        for side_str, size, limit_price in orders:
            if side_str == "YES":
                results.append(self.buy_yes(size=size, market_id=market_id, limit_price=limit_price))
            else:
                results.append(self.buy_no(size=size, market_id=market_id, limit_price=limit_price))
        return results

    # ── CTF split / merge ─────────────────────────────────────────

    def split(self, size: float | int | str, *, market_id: str | None = None) -> None:
        self._engine.split(size, market_id=market_id)

    def merge(self, size: float | int | str, *, market_id: str | None = None) -> None:
        self._engine.merge(size, market_id=market_id)

    # ── Order management ──────────────────────────────────────────

    def cancel(self, order: Order) -> None:
        self._engine.cancel_order(order)

    def cancel_all(self, *, market_id: str | None = None) -> None:
        self._engine.cancel_all_orders(market_id=market_id)

    # ── Settlement ───────────────────────────────────────────────

    def request_merge(self, condition_id: str, amount: float, neg_risk: bool = False) -> None:
        """No-op in backtest. With ``auto_merge`` on (the default) matched YES+NO
        pairs are merged to cash during fill processing; call ``ctx.merge(...)``
        to merge explicitly."""

    def request_redeem(self, condition_id: str, neg_risk: bool = False) -> None:
        """No-op in backtest. Settlement is automatic at resolution."""

    # ── State queries ─────────────────────────────────────────────

    @property
    def open_orders(self) -> list[Order]:
        return self._engine.open_orders

    @property
    def book(self) -> OrderBook:
        return self._engine.current_book

    @property
    def books(self) -> dict[str, OrderBook]:
        return dict(self._engine._books)

    # ── Backwards-compatible aliases ──────────────────────────────

    @property
    def current_book(self) -> OrderBook:
        return self.book

    @property
    def event_books(self) -> dict[str, OrderBook]:
        return self.books


# ── Alpha (signal-level) ─────────────────────────────────────────────


class AlphaStrategy(Strategy):
    """Bar-cadence, signal-driven strategy.

    Override :meth:`on_bar` and set a per-market *target* exposure; the engine
    trades the delta to the target at the next bar's mid. You declare desired
    state, not orders, mirroring ``order_target_percent`` in standard tooling.
    """

    def on_bar(self, ctx: AlphaContext, market: Market, bar: "Bar") -> None:
        """Called once per market per bar at the configured resolution."""

    def on_market_start(
        self, ctx: AlphaContext, market: Market, bar: "Bar",
    ) -> None:
        """Called once when a new market begins, with its first bar."""

    def on_market_end(self, ctx: AlphaContext, market: Market) -> None:
        """Called when a market's bars are exhausted, before settlement."""


class AlphaContext(_ContextBase):
    """Provided to :class:`AlphaStrategy` hooks. Set targets; read state.

    A *target* is signed YES exposure: positive holds YES, negative holds the
    NO side (``|x|`` shares), zero is flat. Targets persist until changed, so
    re-asserting the same target each bar trades nothing.
    """

    def target_weight(self, weight: float, *, market_id: str | None = None) -> None:
        """Target signed position notional as a fraction of current equity.

        ``+0.10`` => long YES worth 10% of equity at the fill mid; ``-0.10`` =>
        the NO side; ``0`` => flat. Shares are recomputed from equity and the
        fill-bar mid at each rebalance (the ``order_target_percent`` convention).
        """
        self._engine.set_target(market_id, weight=float(weight))

    def target_position(self, shares: float, *, market_id: str | None = None) -> None:
        """Target signed share count: ``+N`` => N YES shares, ``-N`` => N NO shares."""
        self._engine.set_target(market_id, shares=float(shares))

    @property
    def bar(self) -> "Bar":
        return self._engine.current_bar

    @property
    def bars(self) -> dict[str, "Bar"]:
        """The latest bar for every market live at this timestamp."""
        return dict(self._engine._bars)


def _is_trade_only(strategy: Strategy) -> bool:
    """True if the strategy doesn't override ``on_book`` (class- or
    instance-level). Used by the engine to auto-route to compact data."""
    return (
        type(strategy).on_book is Strategy.on_book
        and "on_book" not in vars(strategy)
    )


def _is_alpha(strategy: Any) -> bool:
    """True if the strategy is bar-cadence (alpha), routing to the bar engine."""
    return isinstance(strategy, AlphaStrategy)
