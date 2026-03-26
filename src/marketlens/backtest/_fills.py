from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from marketlens.backtest._fees import FeeModel
from marketlens.backtest._types import Fill, Order, OrderSide

if TYPE_CHECKING:
    from marketlens.types.history import TradeEvent
    from marketlens.types.orderbook import OrderBook

_FOUR = Decimal("0.0001")
_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass
class _QueueState:
    market_id: str       # which market this order belongs to
    price: str           # YES-normalized price where order rests
    book_side: str       # "BUY" or "SELL" — side of book the order rests on
    queue_ahead: Decimal  # shares ahead in queue
    level_size: Decimal   # last known total size at this price level


def _order_resting_level(order: Order) -> tuple[str, str]:
    """Return (yes_price, book_side) for where a limit order rests."""
    price = Decimal(order.limit_price)
    if order.side == OrderSide.BUY_YES:
        return str(price.quantize(_FOUR)), "BUY"
    elif order.side == OrderSide.SELL_YES:
        return str(price.quantize(_FOUR)), "SELL"
    elif order.side == OrderSide.BUY_NO:
        return str((_ONE - price).quantize(_FOUR)), "SELL"
    else:  # SELL_NO
        return str((_ONE - price).quantize(_FOUR)), "BUY"


def _depth_at_price(book: OrderBook, price: str, side: str) -> Decimal:
    """Look up total size at a specific price level."""
    levels = book.bids if side == "BUY" else book.asks
    for level in levels:
        if level.price == price:
            return Decimal(level.size)
    return _ZERO


class QueuePositionTracker:
    """Tracks queue-ahead position for open limit orders."""

    def __init__(self) -> None:
        self._states: dict[str, _QueueState] = {}

    def register(self, order: Order, book: OrderBook) -> None:
        price, book_side = _order_resting_level(order)
        depth = _depth_at_price(book, price, book_side)
        self._states[order.id] = _QueueState(
            market_id=order.market_id,
            price=price, book_side=book_side,
            queue_ahead=depth, level_size=depth,
        )

    def unregister(self, order_id: str) -> None:
        self._states.pop(order_id, None)

    def on_trade(self, order_id: str, trade_size: Decimal, trade_price: str, trade_side: str) -> Decimal:
        """Drain queue for a specific order on a matching trade.
        Returns fill-available size (0 if still queued)."""
        state = self._states.get(order_id)
        if state is None:
            return _ZERO

        # Trade side is taker side. SELL taker consumes BUY book side, and vice versa.
        consumed_side = "BUY" if trade_side == "SELL" else "SELL"
        if state.book_side != consumed_side or state.price != trade_price:
            return _ZERO

        state.queue_ahead -= trade_size
        if state.queue_ahead < _ZERO:
            available = min(-state.queue_ahead, trade_size)
            state.queue_ahead = _ZERO
            return available
        return _ZERO

    def on_delta(self, market_id: str, price: str, new_size: Decimal, side: str) -> None:
        """Update queue positions on book level change.

        Any decrease is proportionally attributed to queue positions — we
        cannot reliably separate trade-caused from cancel-caused decreases
        because delta events typically arrive before their corresponding
        trade events (~65-75% of the time, median ~7-28ms ahead).
        """
        norm_price = str(Decimal(price).quantize(_FOUR))
        for state in self._states.values():
            if state.market_id != market_id or state.price != norm_price or state.book_side != side:
                continue

            old_size = state.level_size
            if new_size < old_size and old_size > _ZERO:
                decrease = old_size - new_size
                proportion = state.queue_ahead / old_size
                state.queue_ahead = max(_ZERO, state.queue_ahead - decrease * proportion)

            state.level_size = new_size

    def on_snapshot(self, market_id: str, book: OrderBook) -> None:
        """Re-sync level sizes from full book snapshot."""
        for state in self._states.values():
            if state.market_id != market_id:
                continue
            state.level_size = _depth_at_price(book, state.price, state.book_side)
            state.queue_ahead = min(state.queue_ahead, state.level_size)


class FillSimulator:
    def __init__(
        self,
        fee_model: FeeModel,
        *,
        taker_only: bool = True,
        max_fill_fraction: float = 1.0,
        slippage_bps: int = 0,
        limit_fill_rate: float = 1.0,
        queue_position: bool = False,
    ) -> None:
        self._fee_model = fee_model
        self._taker_only = taker_only
        self._max_fill_fraction = Decimal(str(max_fill_fraction))
        self._slippage_bps = Decimal(str(slippage_bps))
        self._limit_fill_rate = Decimal(str(limit_fill_rate))
        self._tracker = QueuePositionTracker() if queue_position else None

    def register_limit_order(self, order: Order, book: OrderBook) -> None:
        if self._tracker is not None:
            self._tracker.register(order, book)

    def unregister_order(self, order_id: str) -> None:
        if self._tracker is not None:
            self._tracker.unregister(order_id)

    def notify_delta(self, market_id: str, price: str, new_size: str, side: str) -> None:
        if self._tracker is not None:
            self._tracker.on_delta(market_id, price, Decimal(new_size), side)

    def notify_snapshot(self, market_id: str, book: OrderBook) -> None:
        if self._tracker is not None:
            self._tracker.on_snapshot(market_id, book)

    def try_fill_market_order(
        self, order: Order, book: OrderBook, timestamp: int,
    ) -> Fill | None:
        remaining = Decimal(order.size) - Decimal(order.filled_size)
        if remaining <= 0:
            return None

        # BUY_YES / SELL_NO walk asks; SELL_YES / BUY_NO walk bids
        if order.side in (OrderSide.BUY_YES, OrderSide.SELL_NO):
            levels = book.asks
        else:
            levels = book.bids

        total_filled = _ZERO
        total_cost = _ZERO  # in YES-price space

        for level in levels:
            available = Decimal(level.size) * self._max_fill_fraction
            level_price = Decimal(level.price)
            fill = min(remaining - total_filled, available)
            if fill <= 0:
                break
            total_cost += fill * level_price
            total_filled += fill
            if total_filled >= remaining:
                break

        if total_filled == 0:
            return None

        yes_vwap = total_cost / total_filled

        # Convert to the order's price space
        if order.side in (OrderSide.BUY_NO, OrderSide.SELL_NO):
            fill_price = _ONE - yes_vwap
        else:
            fill_price = yes_vwap

        # Apply slippage: worse price for the trader
        if self._slippage_bps != _ZERO:
            slip = fill_price * self._slippage_bps / Decimal("10000")
            if order.side in (OrderSide.BUY_YES, OrderSide.BUY_NO):
                fill_price += slip  # buys fill higher
            else:
                fill_price -= slip  # sells fill lower
            fill_price = max(_ZERO, min(_ONE, fill_price))

        fill_price_str = str(fill_price.quantize(_FOUR))
        fill_size_str = str(total_filled.quantize(_FOUR))
        fee = self._fee_model.calculate(fill_price, total_filled, is_maker=False)

        return Fill(
            order_id=order.id,
            market_id=order.market_id,
            side=order.side,
            price=fill_price_str,
            size=fill_size_str,
            fee=str(fee.quantize(_FOUR)),
            timestamp=timestamp,
            is_maker=False,
        )

    def try_fill_crossing_limit_order(
        self, order: Order, book: OrderBook, timestamp: int,
    ) -> Fill | None:
        """Fill the crossing portion of a limit order as a taker.

        When a limit order arrives at the exchange with a price that
        crosses the spread (e.g. BUY_YES limit >= best_ask), the exchange
        fills it immediately against resting liquidity up to the limit
        price, charging taker fees. Any unfilled remainder would rest
        as a maker order (handled by the caller).

        Returns None if the order does not cross the spread.
        """
        remaining = Decimal(order.size) - Decimal(order.filled_size)
        if remaining <= 0:
            return None

        limit_price = Decimal(order.limit_price)  # type: ignore[arg-type]

        # Determine which side of the book to walk and the price constraint.
        # YES-denominated book: asks sorted ascending, bids sorted descending.
        if order.side == OrderSide.BUY_YES:
            levels = book.asks
            yes_limit = limit_price
            accept = lambda lp: lp <= yes_limit  # noqa: E731
        elif order.side == OrderSide.SELL_YES:
            levels = book.bids
            yes_limit = limit_price
            accept = lambda lp: lp >= yes_limit  # noqa: E731
        elif order.side == OrderSide.BUY_NO:
            # BUY_NO at p ≡ SELL_YES at (1-p): walk bids, accept bid >= (1-p)
            levels = book.bids
            yes_limit = _ONE - limit_price
            accept = lambda lp: lp >= yes_limit  # noqa: E731
        elif order.side == OrderSide.SELL_NO:
            # SELL_NO at p ≡ BUY_YES at (1-p): walk asks, accept ask <= (1-p)
            levels = book.asks
            yes_limit = _ONE - limit_price
            accept = lambda lp: lp <= yes_limit  # noqa: E731
        else:
            return None

        # Walk levels up to the limit price
        total_filled = _ZERO
        total_cost = _ZERO  # in YES-price space

        for level in levels:
            level_price = Decimal(level.price)
            if not accept(level_price):
                break
            available = Decimal(level.size) * self._max_fill_fraction
            fill = min(remaining - total_filled, available)
            if fill <= 0:
                break
            total_cost += fill * level_price
            total_filled += fill
            if total_filled >= remaining:
                break

        if total_filled <= 0:
            return None

        yes_vwap = total_cost / total_filled

        # Convert to the order's price space
        if order.side in (OrderSide.BUY_NO, OrderSide.SELL_NO):
            fill_price = _ONE - yes_vwap
        else:
            fill_price = yes_vwap

        fill_price_str = str(fill_price.quantize(_FOUR))
        fill_size_str = str(total_filled.quantize(_FOUR))
        fee = self._fee_model.calculate(fill_price, total_filled, is_maker=False)

        return Fill(
            order_id=order.id,
            market_id=order.market_id,
            side=order.side,
            price=fill_price_str,
            size=fill_size_str,
            fee=str(fee.quantize(_FOUR)),
            timestamp=timestamp,
            is_maker=False,
        )

    def try_fill_limit_order(
        self,
        order: Order,
        book: OrderBook,
        trade: TradeEvent | None,
        timestamp: int,
    ) -> Fill | None:
        if trade is None:
            return None

        remaining = Decimal(order.size) - Decimal(order.filled_size)
        if remaining <= 0:
            return None

        limit_price = Decimal(order.limit_price)  # type: ignore[arg-type]
        trade_price = Decimal(trade.price)
        trade_size = Decimal(trade.size)

        triggered = False
        if order.side == OrderSide.BUY_YES:
            triggered = trade.side == "SELL" and trade_price <= limit_price
        elif order.side == OrderSide.SELL_YES:
            triggered = trade.side == "BUY" and trade_price >= limit_price
        elif order.side == OrderSide.BUY_NO:
            yes_threshold = _ONE - limit_price
            triggered = trade.side == "BUY" and trade_price >= yes_threshold
        elif order.side == OrderSide.SELL_NO:
            yes_threshold = _ONE - limit_price
            triggered = trade.side == "SELL" and trade_price <= yes_threshold

        if not triggered:
            return None

        if self._tracker is not None:
            trade_price_norm = str(trade_price.quantize(_FOUR))
            available = self._tracker.on_trade(order.id, trade_size, trade_price_norm, trade.side)
            if available <= _ZERO:
                return None
        else:
            available = trade_size * self._limit_fill_rate
        fill_size = min(remaining, available).quantize(_FOUR)
        if fill_size <= _ZERO:
            return None
        fee = self._fee_model.calculate(limit_price, fill_size, is_maker=True)

        return Fill(
            order_id=order.id,
            market_id=order.market_id,
            side=order.side,
            price=str(limit_price.quantize(_FOUR)),
            size=str(fill_size.quantize(_FOUR)),
            fee=str(fee.quantize(_FOUR)),
            timestamp=timestamp,
            is_maker=True,
        )
