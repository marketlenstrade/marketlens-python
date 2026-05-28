"""Unit tests for FillSimulator — limit order crossing and maker/taker classification."""

import pytest

from marketlens.backtest._fees import PolymarketFeeModel, ZeroFeeModel
from marketlens.backtest._fills import FillSimulator
from marketlens.backtest._types import Order, OrderSide, OrderType, OrderStatus


# --- Helpers ---

def _make_book(bids: list[tuple[float, float]], asks: list[tuple[float, float]]):
    """Build a minimal OrderBook-like object from (price, size) tuples."""
    class Level:
        def __init__(self, price, size):
            self.price = float(price)
            self.size = float(size)

    class Book:
        def __init__(self, bids, asks):
            self.bids = [Level(p, s) for p, s in bids]
            self.asks = [Level(p, s) for p, s in asks]
            if bids and asks:
                self.midpoint = (float(bids[0][0]) + float(asks[0][0])) / 2
            else:
                self.midpoint = 0.0

    return Book(bids, asks)


def _make_order(side: OrderSide, size: float, limit_price: float) -> Order:
    return Order(
        id="test-order",
        market_id="test-market",
        side=side,
        order_type=OrderType.LIMIT,
        size=float(size),
        limit_price=float(limit_price),
        submitted_at=0,
        status=OrderStatus.OPEN,
    )


def _sim(fee_model=None) -> FillSimulator:
    return FillSimulator(
        fee_model=fee_model or PolymarketFeeModel.crypto(),
        max_fill_fraction=1.0,
    )


class TestBuyYesCrossing:
    """BUY_YES crosses when limit >= best_ask."""

    def test_no_cross_limit_below_ask(self):
        sim = _sim()
        book = _make_book([(0.48, 100)], [(0.52, 100)])
        order = _make_order(OrderSide.BUY_YES, 50, 0.48)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is None

    def test_cross_limit_equals_ask(self):
        sim = _sim()
        book = _make_book([(0.51, 100)], [(0.52, 100)])
        order = _make_order(OrderSide.BUY_YES, 50, 0.52)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False
        assert fill.size == pytest.approx(50.0)
        assert fill.price <= 0.52 + 1e-9

    def test_cross_limit_above_ask(self):
        sim = _sim()
        book = _make_book([(0.51, 100)], [(0.52, 100)])
        order = _make_order(OrderSide.BUY_YES, 50, 0.55)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False
        assert fill.price == pytest.approx(0.52)  # filled at ask, not limit

    def test_cross_walks_multiple_levels_up_to_limit(self):
        sim = _sim()
        book = _make_book(
            [(0.50, 100)],
            [(0.52, 30), (0.53, 30), (0.55, 100)],
        )
        order = _make_order(OrderSide.BUY_YES, 100, 0.54)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False
        assert fill.size == pytest.approx(60.0)

    def test_cross_partial_fill_at_level(self):
        sim = _sim()
        book = _make_book([(0.50, 100)], [(0.52, 20)])
        order = _make_order(OrderSide.BUY_YES, 50, 0.52)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.size == pytest.approx(20.0)

    def test_taker_fee_charged(self):
        sim = _sim(PolymarketFeeModel.crypto())
        book = _make_book([(0.49, 100)], [(0.50, 100)])
        order = _make_order(OrderSide.BUY_YES, 100, 0.50)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.fee > 0  # taker fee at p=0.50

    def test_empty_ask_book_no_cross(self):
        sim = _sim()
        book = _make_book([(0.50, 100)], [])
        order = _make_order(OrderSide.BUY_YES, 50, 0.55)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is None


class TestSellYesCrossing:
    """SELL_YES crosses when limit <= best_bid."""

    def test_no_cross_limit_above_bid(self):
        sim = _sim()
        book = _make_book([(0.48, 100)], [(0.52, 100)])
        order = _make_order(OrderSide.SELL_YES, 50, 0.50)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is None

    def test_cross_limit_equals_bid(self):
        sim = _sim()
        book = _make_book([(0.48, 100)], [(0.52, 100)])
        order = _make_order(OrderSide.SELL_YES, 50, 0.48)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False
        assert fill.price >= 0.48 - 1e-9


class TestBuyNoCrossing:
    def test_no_cross(self):
        sim = _sim()
        book = _make_book([(0.48, 100)], [(0.52, 100)])
        order = _make_order(OrderSide.BUY_NO, 50, 0.49)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is None

    def test_cross(self):
        sim = _sim()
        book = _make_book([(0.51, 100)], [(0.52, 100)])
        order = _make_order(OrderSide.BUY_NO, 50, 0.49)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False


class TestSellNoCrossing:
    def test_no_cross(self):
        sim = _sim()
        book = _make_book([(0.48, 100)], [(0.52, 100)])
        order = _make_order(OrderSide.SELL_NO, 50, 0.49)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is None

    def test_cross(self):
        sim = _sim()
        book = _make_book([(0.48, 100)], [(0.50, 100)])
        order = _make_order(OrderSide.SELL_NO, 50, 0.49)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False


class TestEdgeCases:
    def test_zero_fee_model_still_marks_taker(self):
        sim = _sim(ZeroFeeModel())
        book = _make_book([(0.49, 100)], [(0.50, 100)])
        order = _make_order(OrderSide.BUY_YES, 50, 0.50)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False
        assert fill.fee == 0

    def test_max_fill_fraction_respected(self):
        sim = FillSimulator(
            fee_model=ZeroFeeModel(),
            max_fill_fraction=0.5,
        )
        book = _make_book([(0.48, 100)], [(0.50, 100)])
        order = _make_order(OrderSide.BUY_YES, 80, 0.50)
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.size == pytest.approx(50.0)  # 100 * 0.5
