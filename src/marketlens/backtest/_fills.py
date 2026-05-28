from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from marketlens.backtest._fees import FeeModel
from marketlens.backtest._types import Fill, Order, OrderSide

if TYPE_CHECKING:
    from marketlens.types.history import TradeEvent
    from marketlens.types.orderbook import OrderBook


# Price levels are stored as NUMERIC(6,4); round to match when comparing or
# keying by price so floating-point representation drift never produces false
# misses against API-supplied values.
_PRICE_DP = 4
_SHARE_DP = 4
_PRICE_EPS = 5e-5  # half a tick at 4 d.p.


def _qp(value) -> float:
    return round(float(value), _PRICE_DP)


def _qs(value) -> float:
    return round(float(value), _SHARE_DP)


@dataclass
class _QueueState:
    market_id: str       # which market this order belongs to
    price: float         # YES-normalized price where order rests (4 d.p.)
    book_side: str       # "BUY" or "SELL" — side of book the order rests on
    queue_ahead: float   # shares ahead in queue
    level_size: float    # last known total size at this price level


def _order_resting_level(order: Order) -> tuple[float, str]:
    """Return (yes_price, book_side) for where a limit order rests."""
    price = order.limit_price
    if order.side == OrderSide.BUY_YES:
        return _qp(price), "BUY"
    elif order.side == OrderSide.SELL_YES:
        return _qp(price), "SELL"
    elif order.side == OrderSide.BUY_NO:
        return _qp(1.0 - price), "SELL"
    else:  # SELL_NO
        return _qp(1.0 - price), "BUY"


def _depth_at_price(book: OrderBook, price: float, side: str) -> float:
    """Look up total size at a specific price level."""
    levels = book.bids if side == "BUY" else book.asks
    for level in levels:
        if abs(level.price - price) < _PRICE_EPS:
            return level.size
    return 0.0


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

    def on_trade(self, order_id: str, trade_size: float, trade_price: float, trade_side: str) -> float:
        """Drain queue for a specific order on a matching trade.
        Returns fill-available size (0 if still queued)."""
        state = self._states.get(order_id)
        if state is None:
            return 0.0

        trade_price_f = float(trade_price)
        trade_size_f = float(trade_size)

        # Trade side is taker side. SELL taker consumes BUY book side, and vice versa.
        consumed_side = "BUY" if trade_side == "SELL" else "SELL"
        if state.book_side != consumed_side or abs(state.price - trade_price_f) >= _PRICE_EPS:
            return 0.0

        state.queue_ahead -= trade_size_f
        if state.queue_ahead < 0.0:
            available = min(-state.queue_ahead, trade_size_f)
            state.queue_ahead = 0.0
            return available
        return 0.0

    def on_delta(self, market_id: str, price: float, new_size: float, side: str) -> None:
        """Update queue positions on book level change.

        Any decrease is proportionally attributed to queue positions — we
        cannot reliably separate trade-caused from cancel-caused decreases
        because delta events typically arrive before their corresponding
        trade events (~65-75% of the time, median ~7-28ms ahead).
        """
        norm_price = _qp(price)
        new_size = float(new_size)
        for state in self._states.values():
            if state.market_id != market_id or abs(state.price - norm_price) >= _PRICE_EPS or state.book_side != side:
                continue

            old_size = state.level_size
            if new_size < old_size and old_size > 0.0:
                decrease = old_size - new_size
                proportion = state.queue_ahead / old_size
                state.queue_ahead = max(0.0, state.queue_ahead - decrease * proportion)

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
        self._max_fill_fraction = float(max_fill_fraction)
        self._slippage_bps = float(slippage_bps)
        self._limit_fill_rate = float(limit_fill_rate)
        self._tracker = QueuePositionTracker() if queue_position else None

    def register_limit_order(self, order: Order, book: OrderBook) -> None:
        if self._tracker is not None:
            self._tracker.register(order, book)

    def unregister_order(self, order_id: str) -> None:
        if self._tracker is not None:
            self._tracker.unregister(order_id)

    def notify_delta(self, market_id: str, price: float, new_size: float, side: str) -> None:
        if self._tracker is not None:
            self._tracker.on_delta(market_id, price, new_size, side)

    def notify_snapshot(self, market_id: str, book: OrderBook) -> None:
        if self._tracker is not None:
            self._tracker.on_snapshot(market_id, book)

    def try_fill_market_order(
        self, order: Order, book: OrderBook, timestamp: int,
    ) -> Fill | None:
        remaining = order.size - order.filled_size
        if remaining <= 0:
            return None

        # BUY_YES / SELL_NO walk asks; SELL_YES / BUY_NO walk bids
        if order.side in (OrderSide.BUY_YES, OrderSide.SELL_NO):
            levels = book.asks
        else:
            levels = book.bids

        total_filled = 0.0
        total_cost = 0.0  # in YES-price space

        for level in levels:
            available = level.size * self._max_fill_fraction
            fill = min(remaining - total_filled, available)
            if fill <= 0:
                break
            total_cost += fill * level.price
            total_filled += fill
            if total_filled >= remaining:
                break

        if total_filled == 0:
            return None

        yes_vwap = total_cost / total_filled

        # Convert to the order's price space
        if order.side in (OrderSide.BUY_NO, OrderSide.SELL_NO):
            fill_price = 1.0 - yes_vwap
        else:
            fill_price = yes_vwap

        # Apply slippage: worse price for the trader
        if self._slippage_bps != 0.0:
            slip = fill_price * self._slippage_bps / 10_000.0
            if order.side in (OrderSide.BUY_YES, OrderSide.BUY_NO):
                fill_price += slip  # buys fill higher
            else:
                fill_price -= slip  # sells fill lower
            fill_price = max(0.0, min(1.0, fill_price))

        # Quantize first, then bail if the rounded size is zero — a tiny
        # last sliver below 1e-4 would otherwise emit ``size=0.0`` and
        # break the engine's avg-fill-price division.
        fill_size_q = _qs(total_filled)
        if fill_size_q <= 0.0:
            return None
        fill_price_q = _qp(fill_price)
        fee = self._fee_model.calculate(fill_price, total_filled, is_maker=False)

        return Fill(
            order_id=order.id,
            market_id=order.market_id,
            side=order.side,
            price=fill_price_q,
            size=fill_size_q,
            fee=_qs(fee),
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
        remaining = order.size - order.filled_size
        if remaining <= 0:
            return None

        limit_price = order.limit_price  # type: ignore[assignment]
        assert limit_price is not None

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
            yes_limit = 1.0 - limit_price
            accept = lambda lp: lp >= yes_limit  # noqa: E731
        elif order.side == OrderSide.SELL_NO:
            # SELL_NO at p ≡ BUY_YES at (1-p): walk asks, accept ask <= (1-p)
            levels = book.asks
            yes_limit = 1.0 - limit_price
            accept = lambda lp: lp <= yes_limit  # noqa: E731
        else:
            return None

        total_filled = 0.0
        total_cost = 0.0

        for level in levels:
            if not accept(level.price):
                break
            available = level.size * self._max_fill_fraction
            fill = min(remaining - total_filled, available)
            if fill <= 0:
                break
            total_cost += fill * level.price
            total_filled += fill
            if total_filled >= remaining:
                break

        if total_filled <= 0:
            return None

        yes_vwap = total_cost / total_filled

        if order.side in (OrderSide.BUY_NO, OrderSide.SELL_NO):
            fill_price = 1.0 - yes_vwap
        else:
            fill_price = yes_vwap

        fill_size_q = _qs(total_filled)
        if fill_size_q <= 0.0:
            return None
        fill_price_q = _qp(fill_price)
        fee = self._fee_model.calculate(fill_price, total_filled, is_maker=False)

        return Fill(
            order_id=order.id,
            market_id=order.market_id,
            side=order.side,
            price=fill_price_q,
            size=fill_size_q,
            fee=_qs(fee),
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

        remaining = order.size - order.filled_size
        if remaining <= 0:
            return None

        limit_price = order.limit_price  # type: ignore[assignment]
        assert limit_price is not None
        trade_price = trade.price
        trade_size = trade.size

        # A resting limit fills only when a taker reaches *its* level. Sweeps
        # at better levels prints land on resting orders at those levels; your
        # order only fills when its own level prints. Match on exact price
        # (4dp quantize), not <=/>=, which over-fills on price-distant sweeps.
        limit_q = _qp(limit_price)
        trade_q = _qp(trade_price)

        triggered = False
        if order.side == OrderSide.BUY_YES:
            triggered = trade.side == "SELL" and abs(trade_q - limit_q) < _PRICE_EPS
        elif order.side == OrderSide.SELL_YES:
            triggered = trade.side == "BUY" and abs(trade_q - limit_q) < _PRICE_EPS
        elif order.side == OrderSide.BUY_NO:
            yes_level = _qp(1.0 - limit_price)
            triggered = trade.side == "BUY" and abs(trade_q - yes_level) < _PRICE_EPS
        elif order.side == OrderSide.SELL_NO:
            yes_level = _qp(1.0 - limit_price)
            triggered = trade.side == "SELL" and abs(trade_q - yes_level) < _PRICE_EPS

        if not triggered:
            return None

        if self._tracker is not None:
            available = self._tracker.on_trade(order.id, trade_size, _qp(trade_price), trade.side)
            if available <= 0.0:
                return None
        else:
            available = trade_size * self._limit_fill_rate
        fill_size = _qs(min(remaining, available))
        if fill_size <= 0.0:
            return None
        fee = self._fee_model.calculate(limit_price, fill_size, is_maker=True)

        return Fill(
            order_id=order.id,
            market_id=order.market_id,
            side=order.side,
            price=_qp(limit_price),
            size=fill_size,
            fee=_qs(fee),
            timestamp=timestamp,
            is_maker=True,
        )
