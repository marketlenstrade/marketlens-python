import json

import httpx
import pytest
from decimal import Decimal

from conftest import BASE_URL, SAMPLE_MARKET, SAMPLE_SERIES
from marketlens import MarketLens, PriceLevel, SnapshotEvent, DeltaEvent, TradeEvent
from marketlens.backtest import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    FeeModel,
    Fill,
    FlatFeeModel,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PolymarketFeeModel,
    Position,
    PositionSide,
    SettlementRecord,
    Strategy,
    StrategyContext,
    ZeroFeeModel,
)
from marketlens.backtest._fills import FillSimulator, QueuePositionTracker, _order_resting_level
from marketlens.backtest._portfolio import Portfolio
from marketlens.backtest._results import _deserialize_config, _serialize_config
from marketlens.types.orderbook import OrderBook


# ── Helpers ──────────────────────────────────────────────────────

def _book(bids, asks, market_id="m1", as_of=1000):
    """Build an OrderBook from (price, size) tuples."""
    bid_levels = [PriceLevel(price=p, size=s) for p, s in bids]
    ask_levels = [PriceLevel(price=p, size=s) for p, s in asks]
    best_bid = bid_levels[0].price if bid_levels else None
    best_ask = ask_levels[0].price if ask_levels else None
    spread = None
    midpoint = None
    if best_bid and best_ask:
        spread = str((Decimal(best_ask) - Decimal(best_bid)).quantize(Decimal("0.0001")))
        midpoint = str(((Decimal(best_bid) + Decimal(best_ask)) / 2).quantize(Decimal("0.0001")))
    bd = str(sum((Decimal(s) for _, s in bids), Decimal("0")).quantize(Decimal("0.0001")))
    ad = str(sum((Decimal(s) for _, s in asks), Decimal("0")).quantize(Decimal("0.0001")))
    return OrderBook(
        market_id=market_id, platform="polymarket", as_of=as_of,
        bids=bid_levels, asks=ask_levels,
        best_bid=best_bid, best_ask=best_ask,
        spread=spread, midpoint=midpoint,
        bid_depth=bd, ask_depth=ad,
        bid_levels=len(bid_levels), ask_levels=len(ask_levels),
    )


def _market_with(overrides):
    return {**SAMPLE_MARKET, **overrides}


def _history_response(*events):
    return {"data": list(events), "meta": {"cursor": None, "has_more": False}}


SNAPSHOT_1 = {
    "type": "snapshot", "t": 1000, "is_reseed": False,
    "bids": [{"price": "0.6500", "size": "200.0000"}, {"price": "0.6400", "size": "150.0000"}],
    "asks": [{"price": "0.6700", "size": "100.0000"}, {"price": "0.6800", "size": "250.0000"}],
}
DELTA_1 = {"type": "delta", "t": 1500, "price": "0.6500", "size": "350.0000", "side": "BUY"}
TRADE_SELL = {"type": "trade", "t": 2000, "id": "t1", "price": "0.6500", "size": "50.0000", "side": "SELL"}
TRADE_BUY = {"type": "trade", "t": 2500, "id": "t2", "price": "0.6700", "size": "80.0000", "side": "BUY"}
SNAPSHOT_2 = {
    "type": "snapshot", "t": 5000, "is_reseed": False,
    "bids": [{"price": "0.6600", "size": "180.0000"}],
    "asks": [{"price": "0.6800", "size": "300.0000"}],
}


# ── Fee Model Tests ──────────────────────────────────────────────

class TestFeeModels:
    def test_crypto_fee_at_midpoint(self):
        fm = PolymarketFeeModel.crypto()
        # fee = 100 * 0.50 * 0.25 * (0.50 * 0.50)^2 = 100 * 0.0078125 = 0.78125
        fee = fm.calculate(Decimal("0.5000"), Decimal("100"), is_maker=False)
        assert fee == Decimal("0.7812")

    def test_crypto_fee_at_extreme(self):
        fm = PolymarketFeeModel.crypto()
        # fee = 100 * 0.99 * 0.25 * (0.99 * 0.01)^2 = 100 * 0.99 * 0.25 * 0.000098 ≈ 0.002426
        fee = fm.calculate(Decimal("0.9900"), Decimal("100"), is_maker=False)
        assert fee == Decimal("0.0024")

    def test_sports_fee_at_midpoint(self):
        fm = PolymarketFeeModel.sports()
        # fee = 100 * 0.50 * 0.0175 * (0.50 * 0.50)^1 = 100 * 0.0021875 = 0.21875
        fee = fm.calculate(Decimal("0.5000"), Decimal("100"), is_maker=False)
        assert fee == Decimal("0.2188")

    def test_polymarket_maker_zero(self):
        fm = PolymarketFeeModel.crypto()
        fee = fm.calculate(Decimal("0.5000"), Decimal("100"), is_maker=True)
        assert fee == Decimal("0")

    def test_for_category_crypto(self):
        fm = PolymarketFeeModel.for_category("Crypto")
        fee = fm.calculate(Decimal("0.5000"), Decimal("100"), is_maker=False)
        assert fee == Decimal("0.7812")

    def test_for_category_other_returns_zero(self):
        fm = PolymarketFeeModel.for_category("Weather")
        fee = fm.calculate(Decimal("0.5000"), Decimal("100"), is_maker=False)
        assert fee == Decimal("0")

    def test_for_category_none_returns_zero(self):
        fm = PolymarketFeeModel.for_category(None)
        fee = fm.calculate(Decimal("0.5000"), Decimal("100"), is_maker=False)
        assert fee == Decimal("0")

    def test_zero_fee_model(self):
        fm = ZeroFeeModel()
        fee = fm.calculate(Decimal("0.5"), Decimal("1000"), is_maker=False)
        assert fee == Decimal("0")

    def test_flat_fee_model(self):
        fm = FlatFeeModel(Decimal("0.01"))
        fee = fm.calculate(Decimal("0.5"), Decimal("100"), is_maker=False)
        assert fee == Decimal("1.0000")


# ── Fill Simulator Tests ─────────────────────────────────────────

class TestFillSimulatorMarket:
    def _sim(self, **kwargs):
        return FillSimulator(ZeroFeeModel(), **kwargs)

    def _order(self, side, size, market_id="m1"):
        return Order(
            id="ord-1", market_id=market_id, side=side,
            order_type=OrderType.MARKET, size=size, submitted_at=1000,
        )

    def test_buy_yes_walks_asks(self):
        book = _book(
            [("0.6500", "200.0000")],
            [("0.6700", "100.0000"), ("0.6800", "250.0000")],
        )
        order = self._order(OrderSide.BUY_YES, "150.0000")
        fill = self._sim().try_fill_market_order(order, book, 1000)
        assert fill is not None
        assert fill.size == "150.0000"
        # VWAP: (100*0.67 + 50*0.68) / 150 = (67 + 34) / 150 = 0.6733...
        expected = (Decimal("100") * Decimal("0.67") + Decimal("50") * Decimal("0.68")) / Decimal("150")
        assert fill.price == str(expected.quantize(Decimal("0.0001")))

    def test_sell_yes_walks_bids(self):
        book = _book(
            [("0.6500", "200.0000"), ("0.6400", "150.0000")],
            [("0.6700", "100.0000")],
        )
        order = self._order(OrderSide.SELL_YES, "100.0000")
        fill = self._sim().try_fill_market_order(order, book, 1000)
        assert fill is not None
        assert fill.price == "0.6500"
        assert fill.size == "100.0000"

    def test_buy_no_walks_bids_inverts(self):
        book = _book(
            [("0.6500", "200.0000")],
            [("0.6700", "100.0000")],
        )
        order = self._order(OrderSide.BUY_NO, "100.0000")
        fill = self._sim().try_fill_market_order(order, book, 1000)
        assert fill is not None
        # YES VWAP from bids = 0.65, NO price = 1 - 0.65 = 0.35
        assert fill.price == "0.3500"
        assert fill.size == "100.0000"

    def test_sell_no_walks_asks_inverts(self):
        book = _book(
            [("0.6500", "200.0000")],
            [("0.6700", "100.0000")],
        )
        order = self._order(OrderSide.SELL_NO, "50.0000")
        fill = self._sim().try_fill_market_order(order, book, 1000)
        assert fill is not None
        # YES VWAP from asks = 0.67, NO price = 1 - 0.67 = 0.33
        assert fill.price == "0.3300"

    def test_empty_book_returns_none(self):
        book = _book([], [])
        order = self._order(OrderSide.BUY_YES, "100.0000")
        fill = self._sim().try_fill_market_order(order, book, 1000)
        assert fill is None

    def test_partial_fill(self):
        book = _book([], [("0.7000", "30.0000")])
        order = self._order(OrderSide.BUY_YES, "100.0000")
        fill = self._sim().try_fill_market_order(order, book, 1000)
        assert fill is not None
        assert fill.size == "30.0000"

    def test_max_fill_fraction(self):
        book = _book([], [("0.7000", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "100.0000")
        fill = self._sim(max_fill_fraction=0.5).try_fill_market_order(order, book, 1000)
        assert fill is not None
        assert fill.size == "50.0000"


class TestFillSimulatorLimit:
    def _sim(self):
        return FillSimulator(ZeroFeeModel())

    def _order(self, side, size, limit_price, market_id="m1"):
        return Order(
            id="ord-1", market_id=market_id, side=side,
            order_type=OrderType.LIMIT, size=size, limit_price=limit_price,
            submitted_at=1000, status=OrderStatus.OPEN,
        )

    def _trade(self, side, price, size="50.0000"):
        return TradeEvent(type="trade", t=2000, id="t1", price=price, size=size, side=side)

    def test_buy_yes_fills_on_sell_trade_at_limit(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        trade = self._trade("SELL", "0.6500")
        fill = self._sim().try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.price == "0.6500"
        assert fill.size == "50.0000"
        assert fill.is_maker is True

    def test_buy_yes_no_fill_on_buy_trade(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        trade = self._trade("BUY", "0.6700")
        fill = self._sim().try_fill_limit_order(order, book, trade, 2000)
        assert fill is None

    def test_buy_yes_no_fill_above_limit(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6400")
        trade = self._trade("SELL", "0.6500")
        fill = self._sim().try_fill_limit_order(order, book, trade, 2000)
        assert fill is None

    def test_sell_yes_fills_on_buy_trade(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.SELL_YES, "50.0000", "0.6700")
        trade = self._trade("BUY", "0.6700")
        fill = self._sim().try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.price == "0.6700"

    def test_buy_no_fills_on_buy_trade(self):
        # BUY_NO at q=0.35 → YES threshold = 1 - 0.35 = 0.65
        # Fills when BUY trade price >= 0.65
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_NO, "50.0000", "0.3500")
        trade = self._trade("BUY", "0.6700")
        fill = self._sim().try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.price == "0.3500"

    def test_sell_no_fills_on_sell_trade(self):
        # SELL_NO at q=0.35 → YES threshold = 1 - 0.35 = 0.65
        # Fills when SELL trade price <= 0.65
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.SELL_NO, "50.0000", "0.3500")
        trade = self._trade("SELL", "0.6500")
        fill = self._sim().try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.price == "0.3500"

    def test_fill_size_capped_by_trade(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "200.0000", "0.6500")
        trade = self._trade("SELL", "0.6500", size="30.0000")
        fill = self._sim().try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.size == "30.0000"

    def test_no_fill_without_trade(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        fill = self._sim().try_fill_limit_order(order, book, None, 2000)
        assert fill is None


# ── Portfolio Tests ──────────────────────────────────────────────

class TestPortfolio:
    def test_initial_state(self):
        p = Portfolio("10000.0000")
        assert p.cash == "10000.0000"
        assert p.equity == "10000.0000"
        pos = p.position("m1")
        assert pos.side == PositionSide.FLAT
        assert pos.shares == "0.0000"

    def test_buy_yes_updates_cash_and_position(self):
        p = Portfolio("10000.0000")
        fill = Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_YES,
            price="0.6500", size="100.0000", fee="0.0000", timestamp=1000, is_maker=False,
        )
        p.apply_fill(fill)
        assert p.cash == "9935.0000"  # 10000 - 65
        pos = p.position("m1")
        assert pos.side == PositionSide.YES
        assert pos.shares == "100.0000"
        assert pos.avg_entry_price == "0.6500"

    def test_sell_yes_realizes_pnl(self):
        p = Portfolio("10000.0000")
        p.apply_fill(Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_YES,
            price="0.6500", size="100.0000", fee="0.0000", timestamp=1000, is_maker=False,
        ))
        p.apply_fill(Fill(
            order_id="o2", market_id="m1", side=OrderSide.SELL_YES,
            price="0.7000", size="100.0000", fee="0.0000", timestamp=2000, is_maker=False,
        ))
        # cash: 10000 - 65 + 70 = 10005
        assert p.cash == "10005.0000"
        pos = p.position("m1")
        assert pos.side == PositionSide.FLAT
        assert pos.realized_pnl == "5.0000"

    def test_buy_no_updates_position(self):
        p = Portfolio("10000.0000")
        fill = Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_NO,
            price="0.3500", size="100.0000", fee="0.0000", timestamp=1000, is_maker=False,
        )
        p.apply_fill(fill)
        assert p.cash == "9965.0000"  # 10000 - 35
        pos = p.position("m1")
        assert pos.side == PositionSide.NO
        assert pos.shares == "100.0000"
        assert pos.avg_entry_price == "0.3500"

    def test_fees_deducted(self):
        p = Portfolio("10000.0000")
        fill = Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_YES,
            price="0.6500", size="100.0000", fee="0.5000", timestamp=1000, is_maker=False,
        )
        p.apply_fill(fill)
        assert p.cash == "9934.5000"  # 10000 - 65 - 0.5
        assert p.total_fees == "0.5000"

    def test_settle_yes_win(self):
        from marketlens.types.market import Market
        p = Portfolio("10000.0000")
        p.apply_fill(Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_YES,
            price="0.6500", size="100.0000", fee="0.0000", timestamp=1000, is_maker=False,
        ))
        market = Market.model_validate(_market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "resolved_at": 5000,
        }))
        record = p.settle_market(market, 5000)
        assert record is not None
        assert record.settlement_price == "1.0000"
        assert record.pnl == "35.0000"  # (1.0 - 0.65) * 100
        # Cash: 9935 + 100 = 10035
        assert p.cash == "10035.0000"

    def test_settle_yes_loss(self):
        from marketlens.types.market import Market
        p = Portfolio("10000.0000")
        p.apply_fill(Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_YES,
            price="0.6500", size="100.0000", fee="0.0000", timestamp=1000, is_maker=False,
        ))
        market = Market.model_validate(_market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "No", "winning_outcome_index": 1,
            "resolved_at": 5000,
        }))
        record = p.settle_market(market, 5000)
        assert record is not None
        assert record.settlement_price == "0.0000"
        assert record.pnl == "-65.0000"
        assert p.cash == "9935.0000"  # unchanged from after buy

    def test_settle_no_win(self):
        from marketlens.types.market import Market
        p = Portfolio("10000.0000")
        p.apply_fill(Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_NO,
            price="0.3500", size="100.0000", fee="0.0000", timestamp=1000, is_maker=False,
        ))
        market = Market.model_validate(_market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "No", "winning_outcome_index": 1,
            "resolved_at": 5000,
        }))
        record = p.settle_market(market, 5000)
        assert record is not None
        assert record.settlement_price == "1.0000"
        assert record.pnl == "65.0000"
        assert p.cash == "10065.0000"  # 9965 + 100

    def test_settle_flat_returns_none(self):
        from marketlens.types.market import Market
        p = Portfolio("10000.0000")
        market = Market.model_validate(_market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "resolved_at": 5000,
        }))
        assert p.settle_market(market, 5000) is None

    def test_mark_to_market(self):
        p = Portfolio("10000.0000")
        p.apply_fill(Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_YES,
            price="0.6500", size="100.0000", fee="0.0000", timestamp=1000, is_maker=False,
        ))
        book = _book([("0.7000", "200.0000")], [("0.7200", "100.0000")])
        p.mark_to_market("m1", book)
        pos = p.position("m1")
        assert pos.unrealized_pnl == "5.0000"  # (0.70 - 0.65) * 100

    def test_can_sell(self):
        p = Portfolio("10000.0000")
        p.apply_fill(Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_YES,
            price="0.6500", size="100.0000", fee="0.0000", timestamp=1000, is_maker=False,
        ))
        assert p.can_sell("m1", OrderSide.SELL_YES, Decimal("100")) is True
        assert p.can_sell("m1", OrderSide.SELL_YES, Decimal("101")) is False
        assert p.can_sell("m1", OrderSide.SELL_NO, Decimal("1")) is False


# ── Strategy Context Tests ───────────────────────────────────────

class TestStrategyContext:
    def test_buy_yes_creates_correct_order(self, mock_api, client):
        """ctx.buy_yes() should submit a BUY_YES market order."""
        m1 = _market_with({"id": "m1", "status": "resolved",
                           "winning_outcome": "Yes", "winning_outcome_index": 0,
                           "open_time": 1000, "close_time": 6000, "resolved_at": 6000})

        class BuyOnce(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    order = ctx.buy_yes(size="100.0000")
                    assert order.side == OrderSide.BUY_YES
                    assert order.order_type == OrderType.MARKET

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))
        result = client.backtest(BuyOnce(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert result.total_trades == 1

    def test_sell_validation_raises(self, mock_api, client):
        """Selling more than held should raise ValueError."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        class SellWithoutHolding(Strategy):
            def on_book(self, ctx, market, book):
                ctx.sell_yes(size="100.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))
        with pytest.raises(ValueError, match="Cannot sell"):
            client.backtest(SellWithoutHolding(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)

    def test_limit_price_validation(self, mock_api, client):
        """Limit price outside (0, 1) should raise ValueError."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        class BadLimit(Strategy):
            def on_book(self, ctx, market, book):
                ctx.buy_yes(size="100.0000", limit_price="1.5000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))
        with pytest.raises(ValueError, match="Limit price must be in"):
            client.backtest(BadLimit(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)

    def test_buy_accepts_numeric_size_and_limit_price(self, mock_api, client):
        """size/limit_price accept float/int/Decimal so strategies don't have
        to wrap arithmetic in ``str(...)``. The engine normalizes to its
        canonical decimal-string form internally."""
        from decimal import Decimal as D

        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})
        captured: list[Any] = []

        class MixedTypes(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.open_orders:
                    return
                # float size, no limit (market order)
                o1 = ctx.buy_yes(size=12.5)
                # int size + float limit_price (limit order)
                o2 = ctx.buy_yes(size=7, limit_price=0.42)
                # Decimal size + Decimal limit_price
                o3 = ctx.buy_yes(size=D("3.25"), limit_price=D("0.55"))
                captured.extend([o1, o2, o3])

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))
        client.backtest(
            MixedTypes(), "m1", after=1000, before=6000,
            initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0,
        )

        assert [o.size for o in captured] == ["12.5", "7", "3.25"]
        assert [o.limit_price for o in captured] == [None, "0.42", "0.55"]

    def test_cancel_order(self, mock_api, client):
        """Cancelling a limit order should set status to CANCELLED."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})
        cancelled = []

        class PlaceAndCancel(Strategy):
            def on_book(self, ctx, market, book):
                if not ctx.open_orders and not cancelled:
                    order = ctx.buy_yes(size="100.0000", limit_price="0.6000")
                    ctx.cancel(order)
                    cancelled.append(order)

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))
        client.backtest(PlaceAndCancel(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert cancelled[0].status == OrderStatus.CANCELLED


# ── Engine Integration Tests ─────────────────────────────────────

class TestEngineIntegration:
    def test_buy_and_settle_yes_win(self, mock_api, client):
        """Buy YES on first book, market resolves YES → positive P&L."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class BuyFirst(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="100.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        result = client.backtest(BuyFirst(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert result.total_trades == 1
        assert Decimal(result.total_pnl) > 0
        assert len(result.settlements_df()) == 1
        assert result.settlements_df()["pnl"].iloc[0] > 0

    def test_buy_and_settle_yes_loss(self, mock_api, client):
        """Buy YES, market resolves NO → negative P&L."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "No", "winning_outcome_index": 1,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class BuyFirst(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="100.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        result = client.backtest(BuyFirst(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert Decimal(result.total_pnl) < 0

    def test_sell_before_settlement(self, mock_api, client):
        """Buy then sell before settlement → realized P&L from trade, not settlement."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class BuyAndSell(Strategy):
            def __init__(self):
                self._bought = False

            def on_book(self, ctx, market, book):
                if not self._bought:
                    ctx.buy_yes(size="100.0000")
                    self._bought = True
                elif ctx.position().side != "FLAT":
                    ctx.sell_yes(size="100.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1, DELTA_1, SNAPSHOT_2)))

        result = client.backtest(BuyAndSell(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0, settlement_delay_ms=0)
        assert result.total_trades == 2
        # No settlement since position is flat
        assert len(result.settlements_df()) == 0

    def test_limit_order_fills_on_trade(self, mock_api, client):
        """Limit BUY_YES fills when a matching SELL trade occurs."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class LimitBuyer(Strategy):
            def on_market_start(self, ctx, market, book):
                ctx.buy_yes(size="50.0000", limit_price="0.6500")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                SNAPSHOT_1, TRADE_SELL, SNAPSHOT_2)))

        result = client.backtest(LimitBuyer(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        # TRADE_SELL is side=SELL at price 0.65, should trigger BUY_YES limit at 0.65
        assert result.total_trades == 1

    def test_limit_order_no_fill_wrong_side(self, mock_api, client):
        """Limit BUY_YES should NOT fill on a BUY trade."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class LimitBuyer(Strategy):
            def on_market_start(self, ctx, market, book):
                ctx.buy_yes(size="50.0000", limit_price="0.6500")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                SNAPSHOT_1, TRADE_BUY, SNAPSHOT_2)))

        result = client.backtest(LimitBuyer(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert result.total_trades == 0

    def test_multi_market_series(self, mock_api, client):
        """Backtest across a rolling series with multiple markets."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 3000, "resolved_at": 3000,
        })
        m2 = _market_with({
            "id": "m2", "status": "resolved",
            "winning_outcome": "No", "winning_outcome_index": 1,
            "open_time": 3000, "close_time": 6000, "resolved_at": 6000,
        })

        class BuyEveryMarket(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="100.0000")

        # Market ID 404, fall back to series
        mock_api.get("/markets/btc-daily").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"},
            }))
        mock_api.get("/series/btc-daily").mock(
            return_value=httpx.Response(200, json=SAMPLE_SERIES))
        mock_api.get("/series/btc-daily/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [m1, m2], "meta": {"cursor": None, "has_more": False},
            }))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))
        mock_api.get("/markets/m2/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_2)))

        result = client.backtest(
            BuyEveryMarket(), "btc-daily", status="resolved",
            initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0,
        )
        assert result.markets_traded == 2
        assert len(result.settlements_df()) == 2

    def test_on_market_lifecycle(self, mock_api, client):
        """on_market_start and on_market_end are called correctly."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})
        calls = []

        class LifecycleTracker(Strategy):
            def on_market_start(self, ctx, market, book):
                calls.append("start")

            def on_book(self, ctx, market, book):
                calls.append("book")

            def on_market_end(self, ctx, market):
                calls.append("end")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1, DELTA_1)))

        client.backtest(LifecycleTracker(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert calls == ["start", "book", "book", "end"]

    def test_on_trade_called(self, mock_api, client):
        """on_trade is called with trade events."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})
        trades_seen = []

        class TradeTracker(Strategy):
            def on_trade(self, ctx, market, book, trade):
                trades_seen.append(trade)

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                SNAPSHOT_1, TRADE_SELL, TRADE_BUY)))

        client.backtest(TradeTracker(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert len(trades_seen) == 2
        assert trades_seen[0].side == "SELL"
        assert trades_seen[1].side == "BUY"

    def test_on_fill_called(self, mock_api, client):
        """on_fill is called when an order fills."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})
        fills_seen = []

        class FillTracker(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="50.0000")

            def on_fill(self, ctx, market, fill):
                fills_seen.append(fill)

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        client.backtest(FillTracker(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert len(fills_seen) == 1
        assert fills_seen[0].side == OrderSide.BUY_YES

    def test_cancel_after_expires_order(self, mock_api, client):
        """Orders with cancel_after should be expired when time passes."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        class ExpireTest(Strategy):
            def on_market_start(self, ctx, market, book):
                ctx.buy_yes(size="50.0000", limit_price="0.6000", cancel_after=1200)

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1, DELTA_1)))

        result = client.backtest(ExpireTest(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        # DELTA_1 is at t=1500 > cancel_after=1200, so order should expire
        assert result.total_trades == 0
        orders = result.orders_df()
        assert orders["status"].iloc[0] == "EXPIRED"

    def test_empty_book_market_order_cancelled(self, mock_api, client):
        """Market order against empty book should be cancelled."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})
        empty_snapshot = {
            "type": "snapshot", "t": 1000, "is_reseed": False,
            "bids": [], "asks": [],
        }

        class BuyEmpty(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="100.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(empty_snapshot)))

        result = client.backtest(BuyEmpty(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert result.total_trades == 0
        assert result.orders_df()["status"].iloc[0] == "CANCELLED"

    def test_buy_no_strategy(self, mock_api, client):
        """BUY_NO should create a NO position and settle correctly."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "No", "winning_outcome_index": 1,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class BuyNo(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_no(size="100.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        result = client.backtest(BuyNo(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert result.total_trades == 1
        settlements = result.settlements_df()
        assert len(settlements) == 1
        assert settlements["side"].iloc[0] == "NO"
        assert settlements["pnl"].iloc[0] > 0  # NO wins

    def test_custom_initial_cash(self, mock_api, client):
        """Custom initial_cash should be respected."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        class Noop(Strategy):
            pass

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        result = client.backtest(Noop(), "m1", after=1000, before=6000, initial_cash="50000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert result.total_pnl == "0.0000"
        assert result.total_return == 0.0

    def test_insufficient_cash_cancels_order(self, mock_api, client):
        """Buy order exceeding cash should be cancelled, not filled."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        class OverBuy(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    # 100k shares at ~0.67 = ~67k USDC, but only 100 cash
                    ctx.buy_yes(size="100000.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        result = client.backtest(
            OverBuy(), "m1", after=1000, before=6000,
            initial_cash="100.0000", latency_ms=0, limit_fill_rate=1.0,
        )
        assert result.total_trades == 0
        assert result.cash_rejected == 1
        assert result.orders_df()["status"].iloc[0] == "CANCELLED"
        assert result.summary()["cash_rejected"] == 1


# ── Results Tests ────────────────────────────────────────────────

class TestBacktestResult:
    def _run_simple(self, mock_api, client, winning_index=0):
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes" if winning_index == 0 else "No",
            "winning_outcome_index": winning_index,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class BuyFirst(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="100.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))
        return client.backtest(BuyFirst(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)

    def test_summary_keys(self, mock_api, client):
        result = self._run_simple(mock_api, client)
        s = result.summary()
        expected_keys = {
            "total_pnl", "total_return", "win_rate", "profit_factor",
            "max_drawdown", "sharpe_ratio", "sortino_ratio",
            "expectancy", "avg_win", "avg_loss", "payoff_ratio",
            "avg_holding_ms", "capital_utilization", "max_drawdown_duration_ms",
            "total_trades", "markets_traded",
            "total_fees", "fee_drag_bps", "avg_entry_price",
        }
        assert set(s.keys()) == expected_keys

    def test_repr(self, mock_api, client):
        result = self._run_simple(mock_api, client)
        r = repr(result)
        assert "BacktestResult(" in r
        assert "total_pnl" in r

    def test_trades_df_columns(self, mock_api, client):
        result = self._run_simple(mock_api, client)
        df = result.trades_df()
        assert len(df) == 1
        assert set(df.columns) >= {"market_id", "side", "price", "size", "fee", "is_maker"}

    def test_orders_df_columns(self, mock_api, client):
        result = self._run_simple(mock_api, client)
        df = result.orders_df()
        assert len(df) == 1
        assert "status" in df.columns

    def test_settlements_df_columns(self, mock_api, client):
        result = self._run_simple(mock_api, client)
        df = result.settlements_df()
        assert len(df) == 1
        expected = {"market_id", "side", "shares", "avg_entry_price",
                    "settlement_price", "pnl", "fees", "winning_outcome", "resolved_at"}
        assert set(df.columns) >= expected

    def test_equity_df(self, mock_api, client):
        result = self._run_simple(mock_api, client)
        df = result.equity_df()
        assert len(df) >= 1
        assert "cash" in df.columns
        assert "equity" in df.columns

    def test_to_dataframe_alias(self, mock_api, client):
        result = self._run_simple(mock_api, client)
        df1 = result.to_dataframe()
        df2 = result.settlements_df()
        assert df1.equals(df2)

    def test_win_rate(self, mock_api, client):
        result = self._run_simple(mock_api, client, winning_index=0)
        assert result.win_rate == 1.0

    def test_loss_gives_zero_win_rate(self, mock_api, client):
        result = self._run_simple(mock_api, client, winning_index=1)
        assert result.win_rate == 0.0

    def test_no_trades_result(self, mock_api, client):
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        class Noop(Strategy):
            pass

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        result = client.backtest(Noop(), "m1", after=1000, before=6000, initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert result.total_trades == 0
        assert result.win_rate == 0.0
        assert result.profit_factor == 0.0
        assert result.max_drawdown == 0.0
        assert result.sharpe_ratio is None
        assert result.trades_df().empty
        assert result.settlements_df().empty


# ── Persistence Tests ────────────────────────────────────────────

class TestBacktestResultPersistence:
    def _run_simple(self, mock_api, client):
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class BuyFirst(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="100.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1, SNAPSHOT_2)))
        return client.backtest(
            BuyFirst(), "m1",
            after=1000, before=6000,
            initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0,
        )

    def test_config_and_targets_attached(self, mock_api, client):
        result = self._run_simple(mock_api, client)
        assert result.config.initial_cash == "10000.0000"
        assert result.config.latency_ms == 0
        assert result.targets == {
            "id": "m1", "after": 1000, "before": 6000, "data_dir": None,
        }
        assert result.initial_cash == "10000.0000"

    def test_save_overwrite_behavior(self, mock_api, client, tmp_path):
        result = self._run_simple(mock_api, client)
        out = tmp_path / "run1"
        result.save(out)
        assert (out / "manifest.json").exists()

        with pytest.raises(FileExistsError):
            result.save(out)

        # overwrite=True clears stray files left in the directory.
        (out / "stray.txt").write_text("x")
        result.save(out, overwrite=True)
        assert not (out / "stray.txt").exists()

    def test_round_trip_full(self, mock_api, client, tmp_path):
        result = self._run_simple(mock_api, client)
        result.save(tmp_path / "run1")
        loaded = BacktestResult.load(tmp_path / "run1")

        # Metrics
        assert loaded.summary() == result.summary()
        assert loaded.cash_rejected == result.cash_rejected
        assert loaded.initial_cash == result.initial_cash

        # DataFrames
        assert result.trades_df().reset_index().equals(loaded.trades_df().reset_index())
        assert result.orders_df().reset_index().equals(loaded.orders_df().reset_index())
        assert result.settlements_df().equals(loaded.settlements_df())
        assert result.equity_df().reset_index().equals(loaded.equity_df().reset_index())
        assert result.by_series() == loaded.by_series()

        # Config + targets
        assert loaded.config.initial_cash == result.config.initial_cash
        assert loaded.config.latency_ms == result.config.latency_ms
        assert loaded.targets == result.targets

        # Orders carry their fills back
        assert len(loaded._orders) == len(result._orders)
        for orig, restored in zip(result._orders, loaded._orders):
            assert restored.id == orig.id
            assert [f.price for f in restored.fills] == [f.price for f in orig.fills]

        # Loaded result has no live portfolio
        assert loaded._portfolio is None

    def test_round_trip_empty_backtest(self, mock_api, client, tmp_path):
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        class Noop(Strategy):
            pass

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        result = client.backtest(
            Noop(), "m1", after=1000, before=6000,
            initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0,
        )
        result.save(tmp_path / "empty")
        loaded = BacktestResult.load(tmp_path / "empty")

        assert loaded.summary() == result.summary()
        assert loaded.trades_df().empty
        assert loaded.settlements_df().empty

    def test_load_rejects_unknown_format_version(self, tmp_path):
        out = tmp_path / "bogus"
        out.mkdir()
        (out / "manifest.json").write_text(json.dumps({"format_version": 999}))
        with pytest.raises(ValueError, match="Unsupported format_version"):
            BacktestResult.load(out)

    def test_custom_fee_model_serializes_as_null(self):
        class CustomFee(FeeModel):
            def calculate(self, price, size, is_maker):
                return Decimal("0")

        cfg = BacktestConfig(initial_cash="100.0000", fee_model=CustomFee())
        out = _serialize_config(cfg)
        assert out["fee_model"] is None
        assert "CustomFee" in out["fee_model_repr"]

        restored = _deserialize_config(out)
        assert restored.fee_model is None  # custom subclass cannot be reconstructed
        assert restored.initial_cash == "100.0000"


# ── Latency Simulation Tests ────────────────────────────────────

class TestLatencySimulation:
    def test_market_order_delayed_by_latency(self, mock_api, client):
        """Market order at t=1000 with 50ms latency fills at t>=1050."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })
        fill_times = []

        class BuyFirst(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="100.0000")

            def on_fill(self, ctx, market, fill):
                fill_times.append(fill.timestamp)

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                SNAPSHOT_1, DELTA_1, SNAPSHOT_2)))

        result = client.backtest(BuyFirst(), "m1", after=1000, before=6000,
                                 initial_cash="10000.0000", latency_ms=50, limit_fill_rate=1.0)
        assert result.total_trades == 1
        # Order submitted at t=1000 (SNAPSHOT_1), activates at t=1050
        # First event after that is DELTA_1 at t=1500
        assert fill_times[0] == 1500

    def test_no_latency_fills_immediately(self, mock_api, client):
        """With latency_ms=0, market order fills on the same event."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })
        fill_times = []

        class BuyFirst(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="100.0000")

            def on_fill(self, ctx, market, fill):
                fill_times.append(fill.timestamp)

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        result = client.backtest(BuyFirst(), "m1", after=1000, before=6000,
                                 initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0)
        assert result.total_trades == 1
        assert fill_times[0] == 1000

    def test_latency_fills_against_submission_book(self, mock_api, client):
        """Order fills against the book at submission time, not activation
        time, even when latency_ms shifts activation to a later event."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class BuyFirst(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="100.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        # SNAPSHOT_1 asks: 0.67/100, 0.68/250  (submission book)
        # SNAPSHOT_2 asks: 0.68/300            (later book — what the OLD
        #                                       behaviour would have used)
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                SNAPSHOT_1, SNAPSHOT_2)))

        result = client.backtest(BuyFirst(), "m1", after=1000, before=6000,
                                 initial_cash="10000.0000", latency_ms=50, limit_fill_rate=1.0)
        assert result.total_trades == 1
        # Fills against SNAPSHOT_1 (submission book) — best ask 0.67.
        fill_price = result.trades_df()["price"].iloc[0]
        assert abs(fill_price - 0.67) < 0.0001

    def test_latency_no_fill_if_only_one_event(self, mock_api, client):
        """With latency and only one event, order stays pending → cancelled."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        class BuyFirst(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="100.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        result = client.backtest(BuyFirst(), "m1", after=1000, before=6000,
                                 initial_cash="10000.0000", latency_ms=50, limit_fill_rate=1.0)
        assert result.total_trades == 0

    def test_latency_limit_order_delayed_activation(self, mock_api, client):
        """Limit order with latency becomes OPEN only after delay."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class LimitBuyer(Strategy):
            def on_market_start(self, ctx, market, book):
                ctx.buy_yes(size="50.0000", limit_price="0.6500")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        # Limit placed at t=1000, activates at t=1050
        # TRADE_SELL at t=2000 (price 0.65) should trigger fill since order is active by then
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                SNAPSHOT_1, DELTA_1, TRADE_SELL, SNAPSHOT_2)))

        result = client.backtest(LimitBuyer(), "m1", after=1000, before=6000,
                                 initial_cash="10000.0000", latency_ms=50, limit_fill_rate=1.0)
        assert result.total_trades == 1

    def test_cancel_pending_order(self, mock_api, client):
        """Cancelling a pending (not yet activated) order should work."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        class CancelPending(Strategy):
            def on_market_start(self, ctx, market, book):
                order = ctx.buy_yes(size="100.0000")
                ctx.cancel(order)

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                SNAPSHOT_1, DELTA_1, SNAPSHOT_2)))

        result = client.backtest(CancelPending(), "m1", after=1000, before=6000,
                                 initial_cash="10000.0000", latency_ms=50, limit_fill_rate=1.0)
        assert result.total_trades == 0
        assert result.orders_df()["status"].iloc[0] == "CANCELLED"


# ── Slippage Buffer Tests ──────────────────────────────────────

class TestSlippageBuffer:
    def _sim(self, slippage_bps):
        return FillSimulator(ZeroFeeModel(), slippage_bps=slippage_bps)

    def _order(self, side, size):
        return Order(
            id="ord-1", market_id="m1", side=side,
            order_type=OrderType.MARKET, size=size, submitted_at=1000,
        )

    def test_buy_yes_slippage_increases_price(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000")
        fill = self._sim(slippage_bps=100).try_fill_market_order(order, book, 1000)
        assert fill is not None
        # Base price = 0.67, slippage = 0.67 * 100/10000 = 0.0067 → 0.6767
        assert Decimal(fill.price) == Decimal("0.6767")

    def test_sell_yes_slippage_decreases_price(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.SELL_YES, "50.0000")
        fill = self._sim(slippage_bps=100).try_fill_market_order(order, book, 1000)
        assert fill is not None
        # Base price = 0.65, slippage = 0.65 * 100/10000 = 0.0065 → 0.6435
        assert Decimal(fill.price) == Decimal("0.6435")

    def test_buy_no_slippage_increases_price(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_NO, "50.0000")
        fill = self._sim(slippage_bps=100).try_fill_market_order(order, book, 1000)
        assert fill is not None
        # NO price = 1 - 0.65 = 0.35, slippage = 0.35 * 100/10000 = 0.0035 → 0.3535
        assert Decimal(fill.price) == Decimal("0.3535")

    def test_zero_slippage_no_change(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000")
        fill = self._sim(slippage_bps=0).try_fill_market_order(order, book, 1000)
        assert fill is not None
        assert Decimal(fill.price) == Decimal("0.6700")

    def test_slippage_in_engine(self, mock_api, client):
        """Slippage applied through the engine."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class BuyFirst(Strategy):
            def on_book(self, ctx, market, book):
                if ctx.position().side == "FLAT":
                    ctx.buy_yes(size="100.0000")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        result = client.backtest(BuyFirst(), "m1", after=1000, before=6000,
                                 initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0, slippage_bps=100)
        fill_price = Decimal(result.trades_df()["price"].iloc[0])
        # Asks: 0.67/100, 0.68/250 → VWAP = 0.6733... + slippage
        assert fill_price > Decimal("0.6700")


# ── Limit Fill Rate Tests ──────────────────────────────────────

class TestLimitFillRate:
    def _sim(self, limit_fill_rate):
        return FillSimulator(ZeroFeeModel(), limit_fill_rate=limit_fill_rate)

    def _order(self, side, size, limit_price):
        return Order(
            id="ord-1", market_id="m1", side=side,
            order_type=OrderType.LIMIT, size=size, limit_price=limit_price,
            submitted_at=1000, status=OrderStatus.OPEN,
        )

    def _trade(self, side, price, size="100.0000"):
        return TradeEvent(type="trade", t=2000, id="t1", price=price, size=size, side=side)

    def test_fill_rate_reduces_size(self):
        """With fill_rate=0.1, only 10% of trade size fills."""
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "200.0000", "0.6500")
        trade = self._trade("SELL", "0.6500", size="100.0000")
        fill = self._sim(limit_fill_rate=0.1).try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.size == "10.0000"  # 100 * 0.1 = 10

    def test_fill_rate_one_gives_full_trade(self):
        """With fill_rate=1.0, full trade size fills (original behavior)."""
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "200.0000", "0.6500")
        trade = self._trade("SELL", "0.6500", size="50.0000")
        fill = self._sim(limit_fill_rate=1.0).try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.size == "50.0000"

    def test_fill_rate_caps_at_remaining(self):
        """Fill size capped by remaining order size even with fill_rate."""
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "5.0000", "0.6500")
        trade = self._trade("SELL", "0.6500", size="100.0000")
        fill = self._sim(limit_fill_rate=0.5).try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.size == "5.0000"  # remaining < trade*rate (50), so fill = remaining

    def test_very_small_fill_rate_rounds_to_zero(self):
        """Tiny fill_rate * small trade → rounds to zero → no fill."""
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "200.0000", "0.6500")
        trade = self._trade("SELL", "0.6500", size="0.0001")
        fill = self._sim(limit_fill_rate=0.01).try_fill_limit_order(order, book, trade, 2000)
        assert fill is None  # 0.0001 * 0.01 = 0.000001 → rounds to 0

    def test_fill_rate_in_engine(self, mock_api, client):
        """Limit fill rate applied through the engine."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        class LimitBuyer(Strategy):
            def on_market_start(self, ctx, market, book):
                ctx.buy_yes(size="50.0000", limit_price="0.6500")

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        # TRADE_SELL: size=50, price=0.65
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                SNAPSHOT_1, TRADE_SELL, SNAPSHOT_2)))

        result = client.backtest(LimitBuyer(), "m1", after=1000, before=6000,
                                 initial_cash="10000.0000", latency_ms=0, limit_fill_rate=0.5)
        assert result.total_trades == 1
        # TRADE_SELL size=50, fill_rate=0.5 → 25 shares filled
        fill_size = Decimal(result.trades_df()["size"].iloc[0])
        assert fill_size == Decimal("25.0000")


# ── Queue Position Tracker Tests ─────────────────────────────

class TestQueuePositionTracker:
    def _order(self, side, size, limit_price, order_id="ord-1"):
        return Order(
            id=order_id, market_id="m1", side=side,
            order_type=OrderType.LIMIT, size=size, limit_price=limit_price,
            submitted_at=1000, status=OrderStatus.OPEN,
        )

    def test_register_sets_queue_from_depth(self):
        """BUY_YES at 0.65, book has 200 at bid 0.65 → queue_ahead=200."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)
        state = tracker._states["ord-1"]
        assert state.queue_ahead == Decimal("200")
        assert state.price == "0.6500"
        assert state.book_side == "BUY"

    def test_trade_drains_queue(self):
        """Trade of 50 drains queue from 200→150, no fill."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        available = tracker.on_trade("ord-1", Decimal("50"), "0.6500", "SELL")
        assert available == Decimal("0")
        assert tracker._states["ord-1"].queue_ahead == Decimal("150")

    def test_trade_fills_when_queue_exhausted(self):
        """Queue=50, trade=100 → fill_available=50, queue=0."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "50.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "100.0000", "0.6500")
        tracker.register(order, book)

        available = tracker.on_trade("ord-1", Decimal("100"), "0.6500", "SELL")
        assert available == Decimal("50")
        assert tracker._states["ord-1"].queue_ahead == Decimal("0")

    def test_cancel_proportional_drain(self):
        """queue=200, level=1000, delta decreases to 800, cancel_portion=200, proportion=0.2, drain=40."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "1000.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)
        # Manually set queue_ahead to 200 (as if we're 200 deep in a 1000-size level)
        tracker._states["ord-1"].queue_ahead = Decimal("200")

        tracker.on_delta("m1", "0.6500", Decimal("800"), "BUY")
        # decrease = 200, proportion = 200/1000 = 0.2
        # queue_ahead -= 200 * 0.2 = 40 → 200 - 40 = 160
        assert tracker._states["ord-1"].queue_ahead == Decimal("160")

    def test_trade_then_delta_both_drain(self):
        """Trade front-drains, then delta proportionally drains the full decrease."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "1000.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        # Trade drains 50 from front
        tracker.on_trade("ord-1", Decimal("50"), "0.6500", "SELL")
        assert tracker._states["ord-1"].queue_ahead == Decimal("950")

        # Delta: level goes from 1000 to 920 (decrease of 80)
        # proportion = 950/1000 = 0.95
        # queue_ahead -= 80 * 0.95 = 76 → 950 - 76 = 874
        tracker.on_delta("m1", "0.6500", Decimal("920"), "BUY")
        assert tracker._states["ord-1"].queue_ahead == Decimal("874")

    def test_level_empties_queue_zeroes(self):
        """Level goes to 0 → queue_ahead → 0."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        tracker.on_delta("m1", "0.6500", Decimal("0"), "BUY")
        assert tracker._states["ord-1"].queue_ahead == Decimal("0")

    def test_level_increase_no_change(self):
        """New orders behind you, queue unchanged."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        tracker.on_delta("m1", "0.6500", Decimal("500"), "BUY")
        assert tracker._states["ord-1"].queue_ahead == Decimal("200")
        assert tracker._states["ord-1"].level_size == Decimal("500")

    def test_snapshot_resyncs(self):
        """Snapshot clamps queue_ahead to level_size."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        # Simulate a book where the level shrank
        new_book = _book([("0.6500", "50.0000")], [("0.6700", "100.0000")])
        tracker.on_snapshot("m1", new_book)
        assert tracker._states["ord-1"].queue_ahead == Decimal("50")
        assert tracker._states["ord-1"].level_size == Decimal("50")

    def test_unregister_stops_tracking(self):
        """After unregister, on_trade returns 0."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)
        tracker.unregister("ord-1")

        available = tracker.on_trade("ord-1", Decimal("300"), "0.6500", "SELL")
        assert available == Decimal("0")
        assert "ord-1" not in tracker._states

    def test_buy_no_resting_level(self):
        """BUY_NO at 0.35 → rests SELL side at 0.65."""
        order = self._order(OrderSide.BUY_NO, "50.0000", "0.3500")
        price, side = _order_resting_level(order)
        assert price == "0.6500"
        assert side == "SELL"

    def test_sell_no_resting_level(self):
        """SELL_NO at 0.35 → rests BUY side at 0.65."""
        order = self._order(OrderSide.SELL_NO, "50.0000", "0.3500")
        price, side = _order_resting_level(order)
        assert price == "0.6500"
        assert side == "BUY"

    def test_trade_wrong_price_no_drain(self):
        """Trade at different price returns 0."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        available = tracker.on_trade("ord-1", Decimal("100"), "0.6400", "SELL")
        assert available == Decimal("0")
        assert tracker._states["ord-1"].queue_ahead == Decimal("200")


# ── Queue Position Fill Integration Tests ─────────────────────

class TestQueuePositionFills:
    def _sim(self, **kwargs):
        return FillSimulator(ZeroFeeModel(), **kwargs)

    def _order(self, side, size, limit_price, order_id="ord-1"):
        return Order(
            id=order_id, market_id="m1", side=side,
            order_type=OrderType.LIMIT, size=size, limit_price=limit_price,
            submitted_at=1000, status=OrderStatus.OPEN,
        )

    def _trade(self, side, price, size="50.0000"):
        return TradeEvent(type="trade", t=2000, id="t1", price=price, size=size, side=side)

    def test_default_uses_limit_fill_rate(self):
        """queue_position=False uses existing limit_fill_rate logic."""
        sim = self._sim(limit_fill_rate=0.5)
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "200.0000", "0.6500")
        trade = self._trade("SELL", "0.6500", size="100.0000")
        fill = sim.try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.size == "50.0000"  # 100 * 0.5

    def test_queue_fills_after_drain(self):
        """Order placed with queue 200, trades drain to 0, next trade fills."""
        sim = self._sim(queue_position=True)
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        sim.register_limit_order(order, book)

        # Trade 1: drain 200, queue goes to 0
        trade1 = TradeEvent(type="trade", t=2000, id="t1", price="0.6500", size="200.0000", side="SELL")
        fill1 = sim.try_fill_limit_order(order, book, trade1, 2000)
        assert fill1 is None  # exactly drained, no overflow

        # Trade 2: queue is 0, this trade overflows → fill
        trade2 = TradeEvent(type="trade", t=3000, id="t2", price="0.6500", size="100.0000", side="SELL")
        fill2 = sim.try_fill_limit_order(order, book, trade2, 3000)
        assert fill2 is not None
        assert fill2.size == "50.0000"  # capped by remaining order size

    def test_queue_no_fill_while_queued(self):
        """Trades not enough to drain queue → no fill."""
        sim = self._sim(queue_position=True)
        book = _book([("0.6500", "500.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        sim.register_limit_order(order, book)

        # Trade of 100 at correct price: queue 500→400, no fill
        trade = self._trade("SELL", "0.6500", size="100.0000")
        fill = sim.try_fill_limit_order(order, book, trade, 2000)
        assert fill is None


class TestLazyReferencePrices:
    """Reference prices should only load when a strategy actually queries them."""

    def _setup_market(self, mock_api, with_underlying: bool):
        """Create a 1-event market market that may or may not have an underlying."""
        market = {
            **SAMPLE_MARKET, "id": "m1",
            "underlying": "BTC" if with_underlying else None,
        }
        snapshot = {
            "type": "snapshot", "t": 1000, "is_reseed": False,
            "bids": [{"price": "0.5000", "size": "10.0000"}],
            "asks": [{"price": "0.6000", "size": "10.0000"}],
        }
        mock_api.get("/markets/m1").mock(
            return_value=httpx.Response(200, json=market)
        )
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(
                200, json={"data": [snapshot],
                           "meta": {"cursor": None, "has_more": False}},
            )
        )

    def test_strategy_without_reference_skips_fetch(self, mock_api, client):
        """Strategy that never calls reference_price() must not trigger
        any /reference/candles requests, even when the market has an
        underlying symbol set. We deliberately do NOT mock the candles
        endpoint — any call would raise AllMockedAssertionError."""
        self._setup_market(mock_api, with_underlying=True)

        class NoRefStrategy(Strategy):
            def on_book(self, ctx, market, book):
                pass  # never queries reference

        result = client.backtest(
            NoRefStrategy(), "m1",
            initial_cash="1000", include_trades=False,
            fees=None, progress=False,
        )
        assert result is not None

    def test_strategy_with_reference_triggers_fetch(self, mock_api, client):
        """When reference_price() IS called, the candles endpoint is hit."""
        self._setup_market(mock_api, with_underlying=True)
        candle = {
            "symbol": "BTC", "timestamp": 999,
            "open": "100", "high": "100", "low": "100", "close": "100",
            "volume": "1",
        }
        ref_route = mock_api.get("/reference/candles").mock(
            return_value=httpx.Response(200, json={
                "data": [candle], "meta": {"cursor": None, "has_more": False, "resolution": "1s"},
            })
        )

        seen_refs: list = []

        class RefStrategy(Strategy):
            def on_book(self, ctx, market, book):
                seen_refs.append(ctx.reference_price())

        result = client.backtest(
            RefStrategy(), "m1",
            initial_cash="1000", include_trades=False,
            fees=None, progress=False,
        )
        assert result is not None
        assert ref_route.call_count >= 1
        assert any(r is not None for r in seen_refs)


class TestPackIntoLanes:
    """Lane packing for structured products (interval graph coloring).

    The aim is to collapse N parallel time-overlapping markets into
    K = peak_concurrency lanes, each lane being a chronological chain
    of time-disjoint markets.
    """

    def _market(self, mid: str, open_time: int, close_time: int):
        from marketlens.types.market import Market
        from conftest import SAMPLE_MARKET
        data = {**SAMPLE_MARKET, "id": mid, "open_time": open_time, "close_time": close_time}
        return Market.model_validate(data)

    def test_disjoint_markets_pack_into_one_lane(self):
        from marketlens.backtest._engine import _pack_into_lanes
        ms = [
            self._market("a", 0,   100),
            self._market("b", 100, 200),
            self._market("c", 200, 300),
        ]
        lanes = _pack_into_lanes(ms)
        assert len(lanes) == 1
        assert [m.id for m in lanes[0]] == ["a", "b", "c"]

    def test_overlapping_markets_split_into_separate_lanes(self):
        from marketlens.backtest._engine import _pack_into_lanes
        ms = [
            self._market("a", 0, 100),
            self._market("b", 50, 150),  # overlaps a
            self._market("c", 60, 160),  # overlaps a + b
        ]
        lanes = _pack_into_lanes(ms)
        # Three concurrent markets at any point → 3 lanes minimum.
        assert len(lanes) == 3
        # Each lane has exactly one of these markets.
        assert sorted(lane[0].id for lane in lanes) == ["a", "b", "c"]

    def test_weekly_rolling_pattern(self):
        """Mirror btc-multi-strikes-weekly: markets that open daily and
        last 7 days. Peak concurrency = 7. Across N opening days we
        expect exactly 7 lanes regardless of N."""
        from marketlens.backtest._engine import _pack_into_lanes
        DAY = 86_400_000  # ms
        WEEK = 7 * DAY
        ms = [
            self._market(f"d{day}", day * DAY, day * DAY + WEEK)
            for day in range(20)  # 20 daily-opening markets, 7-day overlap
        ]
        lanes = _pack_into_lanes(ms)
        assert len(lanes) == 7  # peak overlap = 7 days
        # Within each lane, markets are time-disjoint and ordered.
        for lane in lanes:
            for prev, nxt in zip(lane, lane[1:]):
                assert prev.close_time <= nxt.open_time

    def test_multiple_strikes_per_event_and_rolling_overlap(self):
        """11 strikes/day × 7-day window. Peak concurrency = 11×7 = 77."""
        from marketlens.backtest._engine import _pack_into_lanes
        DAY = 86_400_000
        WEEK = 7 * DAY
        ms = []
        for day in range(20):
            for strike in range(11):
                ms.append(self._market(f"d{day}-s{strike}", day * DAY, day * DAY + WEEK))
        lanes = _pack_into_lanes(ms)
        assert len(lanes) == 77

    def test_untimed_markets_get_own_lanes(self):
        from marketlens.backtest._engine import _pack_into_lanes
        from marketlens.types.market import Market
        from conftest import SAMPLE_MARKET
        # ``b`` has no close_time → can't reason about overlap, isolate it.
        a = self._market("a", 0, 100)
        b_data = {**SAMPLE_MARKET, "id": "b", "open_time": 100, "close_time": None}
        b = Market.model_validate(b_data)
        lanes = _pack_into_lanes([a, b])
        assert len(lanes) == 2


class TestFileStreamWindow:
    """``_make_file_stream`` must apply the user's ``[after, before)`` window
    so bulk-mode replays match streaming-mode replays event-for-event.

    The bug: the file stream today reads the entire parquet (full market
    lifetime) and yields every event regardless of the user's window. For a
    sub-window backtest, this delivers extra events to the strategy compared
    to streaming mode and corrupts the result.
    """

    @staticmethod
    def _write_parquet(path, events: list[dict]) -> None:
        """Write a synthetic history parquet matching the production schema."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        schema = pa.schema([
            ("event_type", pa.string()),
            ("t", pa.int64()),
            ("price", pa.float64()),
            ("size", pa.float64()),
            ("side", pa.string()),
            ("trade_id", pa.string()),
            ("is_reseed", pa.bool_()),
            ("bids", pa.string()),
            ("asks", pa.string()),
        ])
        cols = {f.name: [] for f in schema}
        for e in events:
            for k in cols:
                cols[k].append(e.get(k))
        pq.write_table(pa.table(cols, schema=schema), str(path))

    def _market(self, mid, open_time, close_time):
        from marketlens.types.market import Market
        return Market.model_validate({
            **SAMPLE_MARKET, "id": mid,
            "open_time": open_time, "close_time": close_time,
        })

    def test_make_file_stream_filters_window(self, tmp_path):
        """Events with ``t < after`` are silently replayed (book seeded);
        events with ``t >= after`` are yielded; events with ``t >= before``
        are not yielded. The first yielded event's book must reflect the
        anchor snapshot AND every pre-window delta — i.e. silent replay
        builds the book correctly behind the visibility gate."""
        T_OPEN, T_CLOSE = 1_000, 5_000
        AFTER, BEFORE = 2_000, 4_000

        # Anchor snapshot at t=1000: bid 0.60 @ 100, ask 0.70 @ 100.
        # Pre-window delta at t=1500: add bid 0.55 @ 50.
        # Pre-window delta at t=1501: add ask 0.65 @ 50 (new best ask).
        # First in-window event is a trade at t=2500. The book at that
        # moment must reflect both pre-window deltas (best_ask = 0.65).
        # Post-window delta at t=4500 must NOT be yielded.
        events = [
            {
                "event_type": "snapshot", "t": T_OPEN, "is_reseed": False,
                "price": None, "size": None, "side": None, "trade_id": None,
                "bids": json.dumps([{"price": "0.6000", "size": "100.0000"}]),
                "asks": json.dumps([{"price": "0.7000", "size": "100.0000"}]),
            },
            {
                "event_type": "delta", "t": 1_500,
                "price": 0.55, "size": 50.0, "side": "BUY",
                "trade_id": None, "is_reseed": None, "bids": None, "asks": None,
            },
            {
                "event_type": "delta", "t": 1_501,
                "price": 0.65, "size": 50.0, "side": "SELL",
                "trade_id": None, "is_reseed": None, "bids": None, "asks": None,
            },
            {
                "event_type": "trade", "t": 2_500,
                "price": 0.65, "size": 5.0, "side": "BUY", "trade_id": "t1",
                "is_reseed": None, "bids": None, "asks": None,
            },
            {
                "event_type": "trade", "t": 3_500,
                "price": 0.60, "size": 5.0, "side": "SELL", "trade_id": "t2",
                "is_reseed": None, "bids": None, "asks": None,
            },
            {
                "event_type": "delta", "t": 4_500,
                "price": 0.65, "size": 100.0, "side": "SELL",
                "trade_id": None, "is_reseed": None, "bids": None, "asks": None,
            },
        ]
        market = self._market("mkt", T_OPEN, T_CLOSE)
        path = tmp_path / "history-mkt-compact.parquet"
        self._write_parquet(path, events)

        engine = BacktestEngine(strategy=Strategy())
        yielded = list(engine._make_file_stream(
            [market], str(tmp_path), after_ms=AFTER, before_ms=BEFORE,
        ))

        # Every yielded event lies in [AFTER, BEFORE).
        ts = [evt.t for _, evt, _ in yielded]
        assert ts, "expected at least one yielded event"
        assert all(AFTER <= t < BEFORE for t in ts), (
            f"events outside [{AFTER}, {BEFORE}) were yielded: {ts}"
        )

        # First yielded event's book reflects the anchor + pre-window delta:
        # ask side is 0.65 @ 50 (from t=1501), bid side is 0.60 @ 100 (anchor).
        _, _, first_book = yielded[0]
        assert first_book.best_ask == "0.6500", (
            f"silent replay must apply pre-window delta; got best_ask={first_book.best_ask}"
        )
        assert first_book.best_bid == "0.6000"


class TestStreamingEqualsBulk:
    """End-to-end: same backtest, run via streaming and via bulk-export, must
    produce identical ``BacktestResult.summary()``.

    This is the regression net for the playground.ipynb bug: walk + export
    market sets agree (Fix A), the parquet carries the pre-open anchor and
    both endpoints share one coalescer (Fix C), and the bulk file stream
    applies the user's window (Fix B). When all three are right, the two
    modes are observationally equivalent.
    """

    @staticmethod
    def _write_parquet(path, events: list[dict]) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        schema = pa.schema([
            ("event_type", pa.string()),
            ("t", pa.int64()),
            ("price", pa.float64()),
            ("size", pa.float64()),
            ("side", pa.string()),
            ("trade_id", pa.string()),
            ("is_reseed", pa.bool_()),
            ("bids", pa.string()),
            ("asks", pa.string()),
        ])
        cols = {f.name: [] for f in schema}
        for e in events:
            for k in cols:
                cols[k].append(e.get(k))
        pq.write_table(pa.table(cols, schema=schema), str(path))

    @staticmethod
    def _wire_history(events: list[dict]) -> dict:
        """Convert the parquet-row events to the wire format the SDK expects
        from ``/markets/{id}/orderbook/history``."""
        out = []
        for e in events:
            et = e["event_type"]
            if et == "snapshot":
                out.append({
                    "type": "snapshot",
                    "t": e["t"],
                    "is_reseed": e["is_reseed"],
                    "bids": json.loads(e["bids"]),
                    "asks": json.loads(e["asks"]),
                })
            elif et == "delta":
                out.append({
                    "type": "delta",
                    "t": e["t"],
                    "price": f"{e['price']:.4f}",
                    "size": f"{e['size']:.4f}",
                    "side": e["side"],
                })
            else:
                out.append({
                    "type": "trade",
                    "t": e["t"],
                    "id": e["trade_id"],
                    "price": f"{e['price']:.4f}",
                    "size": f"{e['size']:.4f}",
                    "side": e["side"],
                })
        return {"data": out, "meta": {"cursor": None, "has_more": False}}

    def test_playground_streaming_equals_bulk(self, mock_api, tmp_path):
        """Mirror playground.ipynb's reproduction: a 5-minute rolling-series
        backtest run twice — once via streaming, once via ``data_dir`` — must
        produce identical summaries."""
        T_OPEN, T_CLOSE = 1_776_218_400_000, 1_776_218_700_000  # the playground window
        AFTER_ISO = "2026-04-15T02:00:00Z"
        BEFORE_ISO = "2026-04-15T02:05:00Z"

        market = {
            **SAMPLE_MARKET,
            "id": "mkt-1", "platform": "polymarket",
            "series_id": "btc-rolling",
            "open_time": T_OPEN, "close_time": T_CLOSE,
            "category": "Crypto",
        }
        rolling_series = {
            **SAMPLE_SERIES,
            "id": "btc-rolling", "platform_series_id": "btc-up-or-down-5m",
            "is_rolling": True, "title": "BTC Up or Down 5m",
        }

        # Anchor + a few deltas push the midpoint down to 0.31 (= (0.30+0.32)/2)
        # before the first trade. The strategy enters on that trade.
        events = [
            {"event_type": "snapshot", "t": T_OPEN + 10, "is_reseed": False,
             "price": None, "size": None, "side": None, "trade_id": None,
             "bids": json.dumps([{"price": "0.4500", "size": "100.0000"}]),
             "asks": json.dumps([{"price": "0.4700", "size": "100.0000"}])},
            {"event_type": "delta", "t": T_OPEN + 100,
             "price": 0.45, "size": 0.0, "side": "BUY",
             "trade_id": None, "is_reseed": None, "bids": None, "asks": None},
            {"event_type": "delta", "t": T_OPEN + 101,
             "price": 0.30, "size": 200.0, "side": "BUY",
             "trade_id": None, "is_reseed": None, "bids": None, "asks": None},
            {"event_type": "delta", "t": T_OPEN + 102,
             "price": 0.47, "size": 0.0, "side": "SELL",
             "trade_id": None, "is_reseed": None, "bids": None, "asks": None},
            {"event_type": "delta", "t": T_OPEN + 103,
             "price": 0.32, "size": 100.0, "side": "SELL",
             "trade_id": None, "is_reseed": None, "bids": None, "asks": None},
            {"event_type": "trade", "t": T_OPEN + 200,
             "price": 0.32, "size": 5.0, "side": "BUY", "trade_id": "tr1",
             "is_reseed": None, "bids": None, "asks": None},
            {"event_type": "trade", "t": T_OPEN + 1_000,
             "price": 0.32, "size": 3.0, "side": "BUY", "trade_id": "tr2",
             "is_reseed": None, "bids": None, "asks": None},
        ]

        # ── streaming mocks ─────────────────────────────────────────────
        mock_api.get("/series/btc-up-or-down-5m").mock(
            return_value=httpx.Response(200, json=rolling_series)
        )
        mock_api.get("/markets/btc-up-or-down-5m").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"},
            })
        )
        # walk hits /markets with close_after=T_OPEN+1, open_before=T_CLOSE-1
        # (Fix A's half-open translation). Return the single in-flight market.
        mock_api.get("/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [market],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        mock_api.get(f"/markets/{market['id']}/orderbook/history").mock(
            return_value=httpx.Response(200, json=self._wire_history(events))
        )

        # ── bulk: write the parquet ───────────────────────────────────
        self._write_parquet(
            tmp_path / f"history-{market['id']}-compact.parquet",
            events,
        )

        # ── strategy: enter on first trade with midpoint < 0.35 ────────
        class ValueBuyer(Strategy):
            def on_market_start(self, ctx, market, book):
                self._entered = False

            def on_trade(self, ctx, market, book, trade):
                if self._entered or book.midpoint is None:
                    return
                if Decimal(book.midpoint) < Decimal("0.35"):
                    ctx.buy_yes(size="10")
                    self._entered = True

        client = MarketLens(api_key="mk_test", base_url=BASE_URL)
        try:
            r_stream = client.backtest(
                strategy=ValueBuyer(),
                id="btc-up-or-down-5m",
                initial_cash="10000",
                after=AFTER_ISO, before=BEFORE_ISO,
                progress=False,
            )
            r_bulk = client.backtest(
                strategy=ValueBuyer(),
                id="btc-up-or-down-5m",
                initial_cash="10000",
                after=AFTER_ISO, before=BEFORE_ISO,
                data_dir=str(tmp_path),
                progress=False,
            )
        finally:
            client.close()

        s_stream = r_stream.summary()
        s_bulk = r_bulk.summary()

        # The trade must have entered (proves the strategy fired in both modes).
        assert s_stream["total_trades"] == 1, (
            f"streaming run did not enter; summary={s_stream}"
        )
        # Headline equivalence — every summary key matches.
        assert s_stream == s_bulk, (
            f"streaming and bulk produced different summaries.\n"
            f"  streaming: {s_stream}\n"
            f"  bulk:      {s_bulk}"
        )

    def test_data_dir_missing_triggers_auto_download(self, mock_api, tmp_path):
        """When ``data_dir`` doesn't exist, ``backtest()`` creates it and
        downloads the export before replaying. Re-running with the populated
        directory must skip the download entirely."""
        T_OPEN, T_CLOSE = 1_776_218_400_000, 1_776_218_700_000
        AFTER_ISO = "2026-04-15T02:00:00Z"
        BEFORE_ISO = "2026-04-15T02:05:00Z"

        market = {
            **SAMPLE_MARKET,
            "id": "mkt-1", "platform": "polymarket",
            "series_id": "btc-rolling",
            "open_time": T_OPEN, "close_time": T_CLOSE,
            "category": "Crypto",
        }
        rolling_series = {
            **SAMPLE_SERIES,
            "id": "btc-rolling", "platform_series_id": "btc-up-or-down-5m",
            "is_rolling": True, "title": "BTC Up or Down 5m",
        }

        events = [
            {"event_type": "snapshot", "t": T_OPEN + 10, "is_reseed": False,
             "price": None, "size": None, "side": None, "trade_id": None,
             "bids": json.dumps([{"price": "0.4500", "size": "100.0000"}]),
             "asks": json.dumps([{"price": "0.4700", "size": "100.0000"}])},
            {"event_type": "trade", "t": T_OPEN + 200,
             "price": 0.46, "size": 5.0, "side": "BUY", "trade_id": "tr1",
             "is_reseed": None, "bids": None, "asks": None},
        ]

        # ── Pre-build the parquet bytes the bucket will serve. ──────────
        parquet_path = tmp_path / "_seed.parquet"
        self._write_parquet(parquet_path, events)
        parquet_bytes = parquet_path.read_bytes()
        parquet_path.unlink()

        # ── SDK resolution mocks (same as the streaming-vs-bulk test) ───
        mock_api.get("/series/btc-up-or-down-5m").mock(
            return_value=httpx.Response(200, json=rolling_series)
        )
        mock_api.get("/markets/btc-up-or-down-5m").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"},
            })
        )
        mock_api.get("/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [market],
                "meta": {"cursor": None, "has_more": False},
            })
        )

        # ── Export mocks: market-export 404 → series-export manifest. ───
        mock_api.get("/markets/btc-up-or-down-5m/export").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"},
            })
        )
        bucket_url = "https://bucket.example.com/marketlens/history/mkt-1-compact.parquet"
        series_export = mock_api.get("/series/btc-up-or-down-5m/export").mock(
            return_value=httpx.Response(200, json={
                "ready": [{"market_id": "mkt-1", "url": bucket_url, "events": len(events)}],
                "pending": [],
                "failed": [],
                "events_charged": len(events),
            })
        )
        bucket = mock_api.get(bucket_url).mock(
            return_value=httpx.Response(200, content=parquet_bytes)
        )

        class Noop(Strategy):
            def on_trade(self, ctx, market, book, trade):  # noqa: D401
                pass

        data_dir = tmp_path / "auto"
        assert not data_dir.exists()

        client = MarketLens(api_key="mk_test", base_url=BASE_URL)
        try:
            client.backtest(
                strategy=Noop(),
                id="btc-up-or-down-5m",
                initial_cash="10000",
                after=AFTER_ISO, before=BEFORE_ISO,
                data_dir=str(data_dir),
                progress=False,
            )

            assert data_dir.is_dir(), "backtest must create the data_dir"
            assert (data_dir / "history-mkt-1-compact.parquet").read_bytes() == parquet_bytes
            assert series_export.call_count == 1
            assert bucket.call_count == 1
            # after/before are forwarded as ms-epoch query params, not lost
            # somewhere in the auto-download dispatch.
            export_qs = series_export.calls[0].request.url.params
            assert export_qs.get("after") == str(1_776_218_400_000)
            assert export_qs.get("before") == str(1_776_218_700_000)

            # Re-run with the dir now populated: no further export traffic.
            client.backtest(
                strategy=Noop(),
                id="btc-up-or-down-5m",
                initial_cash="10000",
                after=AFTER_ISO, before=BEFORE_ISO,
                data_dir=str(data_dir),
                progress=False,
            )
            assert series_export.call_count == 1, "second run must not re-download"
            assert bucket.call_count == 1
        finally:
            client.close()
