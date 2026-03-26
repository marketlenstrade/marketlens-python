"""Unit tests for FillSimulator — limit order crossing and maker/taker classification."""

import pytest
from decimal import Decimal
from marketlens.backtest._fees import PolymarketFeeModel, ZeroFeeModel
from marketlens.backtest._fills import FillSimulator
from marketlens.backtest._types import Order, OrderSide, OrderType, OrderStatus


# --- Helpers ---

def _make_book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]):
    """Build a minimal OrderBook-like object from (price, size) tuples."""
    class Level:
        def __init__(self, price, size):
            self.price = price
            self.size = size

    class Book:
        def __init__(self, bids, asks):
            self.bids = [Level(p, s) for p, s in bids]
            self.asks = [Level(p, s) for p, s in asks]
            if bids and asks:
                self.midpoint = str((Decimal(bids[0][0]) + Decimal(asks[0][0])) / 2)
            else:
                self.midpoint = None

    return Book(bids, asks)


def _make_order(side: OrderSide, size: str, limit_price: str) -> Order:
    return Order(
        id="test-order",
        market_id="test-market",
        side=side,
        order_type=OrderType.LIMIT,
        size=size,
        limit_price=limit_price,
        submitted_at=0,
        status=OrderStatus.OPEN,
    )


def _sim(fee_model=None) -> FillSimulator:
    return FillSimulator(
        fee_model=fee_model or PolymarketFeeModel.crypto(),
        max_fill_fraction=1.0,
    )


# --- Crossing detection tests ---
# These test try_fill_crossing_limit_order which should be the new method.
# For now we test the full flow through the simulator.

class TestBuyYesCrossing:
    """BUY_YES crosses when limit >= best_ask."""

    def test_no_cross_limit_below_ask(self):
        """Limit 0.48 < best_ask 0.52 → no crossing fill."""
        sim = _sim()
        book = _make_book([("0.48", "100")], [("0.52", "100")])
        order = _make_order(OrderSide.BUY_YES, "50", "0.48")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is None

    def test_cross_limit_equals_ask(self):
        """Limit 0.52 == best_ask 0.52 → crosses, taker fill."""
        sim = _sim()
        book = _make_book([("0.51", "100")], [("0.52", "100")])
        order = _make_order(OrderSide.BUY_YES, "50", "0.52")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False
        assert fill.size == "50.0000"
        assert Decimal(fill.price) <= Decimal("0.52")

    def test_cross_limit_above_ask(self):
        """Limit 0.55 > best_ask 0.52 → crosses, fills at book price not limit."""
        sim = _sim()
        book = _make_book([("0.51", "100")], [("0.52", "100")])
        order = _make_order(OrderSide.BUY_YES, "50", "0.55")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False
        assert Decimal(fill.price) == Decimal("0.52")  # filled at ask, not limit

    def test_cross_walks_multiple_levels_up_to_limit(self):
        """Limit 0.54: should fill at 0.52 and 0.53, but NOT 0.55."""
        sim = _sim()
        book = _make_book(
            [("0.50", "100")],
            [("0.52", "30"), ("0.53", "30"), ("0.55", "100")],
        )
        order = _make_order(OrderSide.BUY_YES, "100", "0.54")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False
        assert Decimal(fill.size) == Decimal("60")  # 30 + 30, not 100

    def test_cross_partial_fill_at_level(self):
        """Only 20 available at ask, order wants 50 → partial fill of 20."""
        sim = _sim()
        book = _make_book([("0.50", "100")], [("0.52", "20")])
        order = _make_order(OrderSide.BUY_YES, "50", "0.52")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert Decimal(fill.size) == Decimal("20")

    def test_taker_fee_charged(self):
        """Crossing fill must charge taker fees, not zero."""
        sim = _sim(PolymarketFeeModel.crypto())
        book = _make_book([("0.49", "100")], [("0.50", "100")])
        order = _make_order(OrderSide.BUY_YES, "100", "0.50")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert Decimal(fill.fee) > 0  # taker fee at p=0.50

    def test_empty_ask_book_no_cross(self):
        """No asks → no crossing possible."""
        sim = _sim()
        book = _make_book([("0.50", "100")], [])
        order = _make_order(OrderSide.BUY_YES, "50", "0.55")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is None


class TestSellYesCrossing:
    """SELL_YES crosses when limit <= best_bid."""

    def test_no_cross_limit_above_bid(self):
        sim = _sim()
        book = _make_book([("0.48", "100")], [("0.52", "100")])
        order = _make_order(OrderSide.SELL_YES, "50", "0.50")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is None

    def test_cross_limit_equals_bid(self):
        sim = _sim()
        book = _make_book([("0.48", "100")], [("0.52", "100")])
        order = _make_order(OrderSide.SELL_YES, "50", "0.48")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False
        assert Decimal(fill.price) >= Decimal("0.48")


class TestBuyNoCrossing:
    """BUY_NO at limit p is equivalent to SELL_YES at (1-p).
    Crosses when best_bid >= (1-p)."""

    def test_no_cross(self):
        """BUY_NO limit 0.49 → equiv SELL_YES at 0.51. best_bid=0.48 < 0.51 → no cross."""
        sim = _sim()
        book = _make_book([("0.48", "100")], [("0.52", "100")])
        order = _make_order(OrderSide.BUY_NO, "50", "0.49")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is None

    def test_cross(self):
        """BUY_NO limit 0.49 → equiv SELL_YES at 0.51. best_bid=0.51 → crosses."""
        sim = _sim()
        book = _make_book([("0.51", "100")], [("0.52", "100")])
        order = _make_order(OrderSide.BUY_NO, "50", "0.49")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False


class TestSellNoCrossing:
    """SELL_NO at limit p is equivalent to BUY_YES at (1-p).
    Crosses when best_ask <= (1-p)."""

    def test_no_cross(self):
        """SELL_NO limit 0.49 → equiv BUY_YES at 0.51. best_ask=0.52 > 0.51 → no cross."""
        sim = _sim()
        book = _make_book([("0.48", "100")], [("0.52", "100")])
        order = _make_order(OrderSide.SELL_NO, "50", "0.49")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is None

    def test_cross(self):
        """SELL_NO limit 0.49 → equiv BUY_YES at 0.51. best_ask=0.50 → crosses."""
        sim = _sim()
        book = _make_book([("0.48", "100")], [("0.50", "100")])
        order = _make_order(OrderSide.SELL_NO, "50", "0.49")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False


class TestEdgeCases:
    """Edge cases for crossing logic."""

    def test_zero_fee_model_still_marks_taker(self):
        """Even with zero fees, crossing fills should be marked is_maker=False."""
        sim = _sim(ZeroFeeModel())
        book = _make_book([("0.49", "100")], [("0.50", "100")])
        order = _make_order(OrderSide.BUY_YES, "50", "0.50")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert fill.is_maker is False
        assert Decimal(fill.fee) == 0

    def test_max_fill_fraction_respected(self):
        """Crossing should respect max_fill_fraction like market orders."""
        sim = FillSimulator(
            fee_model=ZeroFeeModel(),
            max_fill_fraction=0.5,
        )
        book = _make_book([("0.48", "100")], [("0.50", "100")])
        order = _make_order(OrderSide.BUY_YES, "80", "0.50")
        fill = sim.try_fill_crossing_limit_order(order, book, 1000)
        assert fill is not None
        assert Decimal(fill.size) == Decimal("50")  # 100 * 0.5
