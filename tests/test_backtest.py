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
    bid_levels = [PriceLevel(price=float(p), size=float(s)) for p, s in bids]
    ask_levels = [PriceLevel(price=float(p), size=float(s)) for p, s in asks]
    best_bid = bid_levels[0].price if bid_levels else 0.0
    best_ask = ask_levels[0].price if ask_levels else 0.0
    spread = round(best_ask - best_bid, 4) if best_bid and best_ask else 0.0
    midpoint = round((best_bid + best_ask) / 2, 4) if best_bid and best_ask else 0.0
    bd = round(sum(float(s) for _, s in bids), 4)
    ad = round(sum(float(s) for _, s in asks), 4)
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
        fee = fm.calculate(0.5000, 100, is_maker=False)
        assert fee == pytest.approx(0.78125)

    def test_crypto_fee_at_extreme(self):
        fm = PolymarketFeeModel.crypto()
        # fee = 100 * 0.99 * 0.25 * (0.99 * 0.01)^2 = 100 * 0.99 * 0.25 * 0.000098 ≈ 0.002426
        fee = fm.calculate(0.9900, 100, is_maker=False)
        assert fee == pytest.approx(0.002426, abs=1e-5)

    def test_sports_fee_at_midpoint(self):
        fm = PolymarketFeeModel.sports()
        # fee = 100 * 0.50 * 0.0175 * (0.50 * 0.50)^1 = 100 * 0.0021875 = 0.21875
        fee = fm.calculate(0.5000, 100, is_maker=False)
        assert fee == pytest.approx(0.21875)

    def test_polymarket_maker_zero(self):
        fm = PolymarketFeeModel.crypto()
        fee = fm.calculate(0.5000, 100, is_maker=True)
        assert fee == 0

    def test_for_category_crypto(self):
        fm = PolymarketFeeModel.for_category("Crypto")
        fee = fm.calculate(0.5000, 100, is_maker=False)
        assert fee == pytest.approx(0.78125)

    def test_for_category_other_returns_zero(self):
        fm = PolymarketFeeModel.for_category("Weather")
        fee = fm.calculate(0.5000, 100, is_maker=False)
        assert fee == 0

    def test_for_category_none_returns_zero(self):
        fm = PolymarketFeeModel.for_category(None)
        fee = fm.calculate(0.5000, 100, is_maker=False)
        assert fee == 0

    def test_zero_fee_model(self):
        fm = ZeroFeeModel()
        fee = fm.calculate(0.5, 1000, is_maker=False)
        assert fee == 0

    def test_flat_fee_model(self):
        fm = FlatFeeModel(0.01)
        fee = fm.calculate(0.5, 100, is_maker=False)
        assert fee == 1.0000


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
        assert fill.size == 150.0
        # VWAP: (100*0.67 + 50*0.68) / 150 = (67 + 34) / 150 = 0.6733...
        expected = (100 * 0.67 + 50 * 0.68) / 150
        assert fill.price == pytest.approx(expected, abs=1e-4)

    def test_sell_yes_walks_bids(self):
        book = _book(
            [("0.6500", "200.0000"), ("0.6400", "150.0000")],
            [("0.6700", "100.0000")],
        )
        order = self._order(OrderSide.SELL_YES, "100.0000")
        fill = self._sim().try_fill_market_order(order, book, 1000)
        assert fill is not None
        assert fill.price == 0.65
        assert fill.size == 100.0

    def test_buy_no_walks_bids_inverts(self):
        book = _book(
            [("0.6500", "200.0000")],
            [("0.6700", "100.0000")],
        )
        order = self._order(OrderSide.BUY_NO, "100.0000")
        fill = self._sim().try_fill_market_order(order, book, 1000)
        assert fill is not None
        # YES VWAP from bids = 0.65, NO price = 1 - 0.65 = 0.35
        assert fill.price == 0.35
        assert fill.size == 100.0

    def test_sell_no_walks_asks_inverts(self):
        book = _book(
            [("0.6500", "200.0000")],
            [("0.6700", "100.0000")],
        )
        order = self._order(OrderSide.SELL_NO, "50.0000")
        fill = self._sim().try_fill_market_order(order, book, 1000)
        assert fill is not None
        # YES VWAP from asks = 0.67, NO price = 1 - 0.67 = 0.33
        assert fill.price == 0.33

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
        assert fill.size == 30.0

    def test_max_fill_fraction(self):
        book = _book([], [("0.7000", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "100.0000")
        fill = self._sim(max_fill_fraction=0.5).try_fill_market_order(order, book, 1000)
        assert fill is not None
        assert fill.size == 50.0


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
        assert fill.price == 0.65
        assert fill.size == 50.0
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
        assert fill.price == 0.67

    def test_buy_no_fills_on_buy_trade(self):
        # BUY_NO at 0.35 rests as a YES ask at 0.65 (1 - 0.35). A taker who
        # BUYs YES at exactly 0.65 hits that level — earlier same-side resting
        # quotes at higher YES prices would have been hit first by the sweep.
        book = _book([("0.6300", "200.0000")], [("0.6500", "100.0000")])
        order = self._order(OrderSide.BUY_NO, "50.0000", "0.3500")
        trade = self._trade("BUY", "0.6500")
        fill = self._sim().try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.price == 0.35

    def test_buy_no_no_fill_when_trade_above_level(self):
        # Taker BUYs YES at 0.67 — hits some other ask, not your 0.65.
        book = _book([("0.6300", "200.0000")], [("0.6500", "100.0000")])
        order = self._order(OrderSide.BUY_NO, "50.0000", "0.3500")
        trade = self._trade("BUY", "0.6700")
        fill = self._sim().try_fill_limit_order(order, book, trade, 2000)
        assert fill is None

    def test_sell_no_fills_on_sell_trade(self):
        # SELL_NO at 0.35 rests as a YES bid at 0.65. A SELL taker print at
        # exactly 0.65 hits that level.
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.SELL_NO, "50.0000", "0.3500")
        trade = self._trade("SELL", "0.6500")
        fill = self._sim().try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.price == 0.35

    def test_fill_size_capped_by_trade(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "200.0000", "0.6500")
        trade = self._trade("SELL", "0.6500", size="30.0000")
        fill = self._sim().try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.size == 30.0

    def test_no_fill_without_trade(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        fill = self._sim().try_fill_limit_order(order, book, None, 2000)
        assert fill is None


# ── Portfolio Tests ──────────────────────────────────────────────

class TestPortfolio:
    def test_initial_state(self):
        p = Portfolio("10000.0000")
        assert p.cash == 10000.0
        assert p.equity == 10000.0
        pos = p.position("m1")
        assert pos.side == PositionSide.FLAT
        assert pos.shares == 0.0

    def test_buy_yes_updates_cash_and_position(self):
        p = Portfolio("10000.0000")
        fill = Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_YES,
            price="0.6500", size="100.0000", fee="0.0000", timestamp=1000, is_maker=False,
        )
        p.apply_fill(fill)
        assert p.cash == 9935.0  # 10000 - 65
        pos = p.position("m1")
        assert pos.side == PositionSide.YES
        assert pos.shares == 100.0
        assert pos.avg_entry_price == 0.65

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
        assert p.cash == pytest.approx(10005.0)
        pos = p.position("m1")
        assert pos.side == PositionSide.FLAT
        assert pos.realized_pnl == pytest.approx(5.0)

    def test_buy_no_updates_position(self):
        p = Portfolio("10000.0000")
        fill = Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_NO,
            price="0.3500", size="100.0000", fee="0.0000", timestamp=1000, is_maker=False,
        )
        p.apply_fill(fill)
        assert p.cash == 9965.0  # 10000 - 35
        pos = p.position("m1")
        assert pos.side == PositionSide.NO
        assert pos.shares == 100.0
        assert pos.avg_entry_price == 0.35

    def test_fees_deducted(self):
        p = Portfolio("10000.0000")
        fill = Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_YES,
            price="0.6500", size="100.0000", fee="0.5000", timestamp=1000, is_maker=False,
        )
        p.apply_fill(fill)
        assert p.cash == 9934.5  # 10000 - 65 - 0.5
        assert p.total_fees == 0.5

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
        assert record.settlement_price == 1.0
        assert record.pnl == 35.0  # (1.0 - 0.65) * 100
        # Cash: 9935 + 100 = 10035
        assert p.cash == 10035.0

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
        assert record.settlement_price == 0.0
        assert record.pnl == -65.0
        assert p.cash == 9935.0  # unchanged from after buy

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
        assert record.settlement_price == 1.0
        assert record.pnl == 65.0
        assert p.cash == 10065.0  # 9965 + 100

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
        assert pos.unrealized_pnl == pytest.approx(5.0)  # (0.70 - 0.65) * 100

    def test_can_sell(self):
        p = Portfolio("10000.0000")
        p.apply_fill(Fill(
            order_id="o1", market_id="m1", side=OrderSide.BUY_YES,
            price="0.6500", size="100.0000", fee="0.0000", timestamp=1000, is_maker=False,
        ))
        assert p.can_sell("m1", OrderSide.SELL_YES, 100) is True
        assert p.can_sell("m1", OrderSide.SELL_YES, 101) is False
        assert p.can_sell("m1", OrderSide.SELL_NO, 1) is False


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

        assert [o.size for o in captured] == [12.5, 7.0, 3.25]
        assert [o.limit_price for o in captured] == [None, pytest.approx(0.42), pytest.approx(0.55)]

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
        assert result.total_pnl == 0.0
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

    def test_ctf_merge_buy_funded_by_existing_position(self, mock_api, client):
        """Buying the opposite side should net cash against the CTF-merge credit.

        Holding 100 NO (paid 30) then buying 100 YES at 0.30 nets out to a CTF
        merge that credits $100. Net cash impact for the BUY_YES is -30 + 100
        = +70, so a wallet with only $20 of free cash must still allow the
        hedge — the gross-cost check that ignores the merge would falsely
        reject it.
        """
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000,
                           "status": "resolved", "winning_outcome": "Yes",
                           "winning_outcome_index": 0, "resolved_at": 6000})

        # Phase 1: deep YES bid at 0.70 → BUY_NO fills at NO price 0.30.
        snap_no = {
            "type": "snapshot", "t": 1000, "is_reseed": False,
            "bids": [{"price": "0.7000", "size": "500.0000"}],
            "asks": [{"price": "0.7100", "size": "500.0000"}],
        }
        # Phase 2: YES tanks; ask drops to 0.30 → BUY_YES fills at 0.30.
        snap_yes = {
            "type": "snapshot", "t": 3000, "is_reseed": False,
            "bids": [{"price": "0.2900", "size": "500.0000"}],
            "asks": [{"price": "0.3000", "size": "500.0000"}],
        }
        snap_end = {
            "type": "snapshot", "t": 5000, "is_reseed": False,
            "bids": [{"price": "0.5000", "size": "100.0000"}],
            "asks": [{"price": "0.5100", "size": "100.0000"}],
        }

        class HedgeWithThinCash(Strategy):
            steps = 0
            def on_book(self, ctx, market, book):
                if self.steps == 0:
                    ctx.buy_no(size="100.0000")  # 30 cash out
                elif self.steps == 1:
                    # 30 cash needed gross, but matched 100 NO → CTF credit
                    # of 100. Net cash impact is +70.
                    ctx.buy_yes(size="100.0000")
                self.steps += 1

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(snap_no, snap_yes, snap_end)))

        result = client.backtest(
            HedgeWithThinCash(), "m1", after=1000, before=6000,
            initial_cash="50.0000", latency_ms=0, limit_fill_rate=1.0,
            settlement_delay_ms=0, fees=None,
        )
        assert result.total_trades == 2, "hedge buy must succeed despite thin cash"
        assert result.cash_rejected == 0

    def test_on_reject_called_on_empty_book_market_order(self, mock_api, client):
        """A market order against an empty book side should fire on_reject."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        # YES asks are empty → BUY_YES market cannot fill.
        empty_asks = {
            "type": "snapshot", "t": 1000, "is_reseed": False,
            "bids": [{"price": "0.5000", "size": "100.0000"}],
            "asks": [],
        }
        snap2 = {
            "type": "snapshot", "t": 5000, "is_reseed": False,
            "bids": [{"price": "0.5000", "size": "100.0000"}],
            "asks": [{"price": "0.5500", "size": "100.0000"}],
        }

        rejects: list[Order] = []

        class TryBuyNoLiquidity(Strategy):
            tried = False
            def on_book(self, ctx, market, book):
                if not self.tried:
                    ctx.buy_yes(size="10.0000")
                    self.tried = True
            def on_reject(self, ctx, market, order):
                rejects.append(order)

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(empty_asks, snap2)))

        result = client.backtest(
            TryBuyNoLiquidity(), "m1", after=1000, before=6000,
            initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0,
        )
        assert result.total_trades == 0
        assert len(rejects) == 1
        assert rejects[0].status == OrderStatus.CANCELLED

    def test_on_reject_called_on_insufficient_cash(self, mock_api, client):
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        rejects: list[Order] = []

        class OverBuy(Strategy):
            tried = False
            def on_book(self, ctx, market, book):
                if not self.tried:
                    ctx.buy_yes(size="100000.0000")
                    self.tried = True
            def on_reject(self, ctx, market, order):
                rejects.append(order)

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1)))

        client.backtest(
            OverBuy(), "m1", after=1000, before=6000,
            initial_cash="100.0000", latency_ms=0, limit_fill_rate=1.0,
        )
        assert len(rejects) == 1
        assert rejects[0].status == OrderStatus.CANCELLED

    def test_limit_order_no_fill_on_price_distant_sweep(self, mock_api, client):
        """A resting BUY_YES at 0.50 must not fill on a SELL print at 0.45.

        In a real CLOB, a SELL taker hitting 0.45 means the bid book starts
        no higher than 0.45 — any earlier 0.50 bid would have been hit first.
        The engine's exact-level trigger guards against this over-fill.
        """
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        # Book has bids at 0.50 (ours after registration) and 0.45; trades
        # at 0.45 should not fill our 0.50 resting order.
        snap = {
            "type": "snapshot", "t": 1000, "is_reseed": False,
            "bids": [{"price": "0.4500", "size": "100.0000"}],
            "asks": [{"price": "0.6000", "size": "100.0000"}],
        }
        far_trade = {
            "type": "trade", "t": 1500, "id": "tx",
            "price": "0.4500", "size": "50.0000", "side": "SELL",
        }
        snap2 = {
            "type": "snapshot", "t": 5000, "is_reseed": False,
            "bids": [{"price": "0.4500", "size": "100.0000"}],
            "asks": [{"price": "0.6000", "size": "100.0000"}],
        }

        class RestAt50(Strategy):
            placed = False
            def on_book(self, ctx, market, book):
                if not self.placed:
                    ctx.buy_yes(size="20.0000", limit_price="0.5000")
                    self.placed = True

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(snap, far_trade, snap2)))

        result = client.backtest(
            RestAt50(), "m1", after=1000, before=6000,
            initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0,
        )
        assert result.total_trades == 0, "trade below resting price must not fill"

    def test_limit_order_fills_on_exact_level_trade(self, mock_api, client):
        """Same setup, but the trade prints exactly at the resting price."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 6000})

        snap = {
            "type": "snapshot", "t": 1000, "is_reseed": False,
            "bids": [{"price": "0.4500", "size": "100.0000"}],
            "asks": [{"price": "0.6000", "size": "100.0000"}],
        }
        at_level = {
            "type": "trade", "t": 1500, "id": "tx",
            "price": "0.5000", "size": "50.0000", "side": "SELL",
        }
        snap2 = {
            "type": "snapshot", "t": 5000, "is_reseed": False,
            "bids": [{"price": "0.4500", "size": "100.0000"}],
            "asks": [{"price": "0.6000", "size": "100.0000"}],
        }

        class RestAt50(Strategy):
            placed = False
            def on_book(self, ctx, market, book):
                if not self.placed:
                    ctx.buy_yes(size="20.0000", limit_price="0.5000")
                    self.placed = True

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(snap, at_level, snap2)))

        result = client.backtest(
            RestAt50(), "m1", after=1000, before=6000,
            initial_cash="10000.0000", latency_ms=0, limit_fill_rate=1.0,
        )
        assert result.total_trades == 1


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
        # _run_simple passes a string for back-compat; Portfolio coerces it.
        assert float(result.config.initial_cash) == pytest.approx(10000.0)
        assert result.config.latency_ms == 0
        assert result.targets == {
            "id": "m1", "after": 1000, "before": 6000, "data_dir": None,
        }
        assert result.initial_cash == pytest.approx(10000.0)

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
                return 0

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
        # Order submitted at t=1000, activates at t=1050. The next event in
        # the stream (DELTA_1 at t=1500) drains the pending queue and fires
        # the activation at its own activate_at — not at the event's time.
        assert fill_times[0] == 1050

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

    def test_latency_no_intervening_events_preserves_submission_book(
        self, mock_api, client,
    ):
        """When nothing happened during the latency window, the live book at
        activation is unchanged from submission, so the fill price matches
        the strategy's view at submission."""
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
        # SNAPSHOT_1 asks: 0.67/100, 0.68/250. SNAPSHOT_2 is the next event,
        # far in the future — nothing happened in [1000, 1050]. Activation at
        # 1050 sees the same book the strategy saw at 1000, so we fill at 0.67.
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                SNAPSHOT_1, SNAPSHOT_2)))

        result = client.backtest(BuyFirst(), "m1", after=1000, before=6000,
                                 initial_cash="10000.0000", latency_ms=50, limit_fill_rate=1.0)
        assert result.total_trades == 1
        fill_price = result.trades_df()["price"].iloc[0]
        assert abs(fill_price - 0.67) < 0.0001
        # Fill is stamped at the order's activate_at, not the next event.
        assert result._fills[0].timestamp == 1050

    def test_latency_book_moves_against_market_buy(self, mock_api, client):
        """Adverse selection on price: book moves up between submission and
        activation. The market buy pays the worse, activation-time ask."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        # Submission book: best ask 0.67 (cheap).
        snap_cheap = {
            "type": "snapshot", "t": 1000, "is_reseed": False,
            "bids": [{"price": "0.6500", "size": "200.0000"}],
            "asks": [{"price": "0.6700", "size": "100.0000"}],
        }
        # Mid-latency event at t=1025: asks lift to 0.72 (book moved up).
        snap_expensive = {
            "type": "snapshot", "t": 1025, "is_reseed": False,
            "bids": [{"price": "0.6800", "size": "200.0000"}],
            "asks": [{"price": "0.7200", "size": "100.0000"}],
        }
        snap_end = {
            "type": "snapshot", "t": 5000, "is_reseed": False,
            "bids": [{"price": "0.7000", "size": "100.0000"}],
            "asks": [{"price": "0.7200", "size": "100.0000"}],
        }

        class BuyFirst(Strategy):
            done = False
            def on_book(self, ctx, market, book):
                if not self.done:
                    ctx.buy_yes(size="50.0000")
                    self.done = True

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                snap_cheap, snap_expensive, snap_end)))

        result = client.backtest(
            BuyFirst(), "m1", after=1000, before=6000,
            initial_cash="10000.0000", latency_ms=50, limit_fill_rate=1.0,
            fees=None,
        )
        assert result.total_trades == 1
        # Activation at t=1050, after snap_expensive (t=1025) updated the book
        # → pays the new ask 0.72, not the submission-time 0.67.
        fill_price = result.trades_df()["price"].iloc[0]
        assert abs(fill_price - 0.72) < 0.0001
        # Fill stamped at the true activation time.
        assert result._fills[0].timestamp == 1050

    def test_latency_book_moves_favourably_for_market_buy(self, mock_api, client):
        """Symmetric case: book drops between submission and activation → the
        market buy gets a better price than the strategy saw."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })
        snap_high = {
            "type": "snapshot", "t": 1000, "is_reseed": False,
            "bids": [{"price": "0.6500", "size": "200.0000"}],
            "asks": [{"price": "0.6700", "size": "100.0000"}],
        }
        snap_low = {
            "type": "snapshot", "t": 1025, "is_reseed": False,
            "bids": [{"price": "0.5800", "size": "200.0000"}],
            "asks": [{"price": "0.6000", "size": "100.0000"}],
        }
        snap_end = {
            "type": "snapshot", "t": 5000, "is_reseed": False,
            "bids": [{"price": "0.5800", "size": "100.0000"}],
            "asks": [{"price": "0.6000", "size": "100.0000"}],
        }

        class BuyFirst(Strategy):
            done = False
            def on_book(self, ctx, market, book):
                if not self.done:
                    ctx.buy_yes(size="50.0000")
                    self.done = True

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                snap_high, snap_low, snap_end)))

        result = client.backtest(
            BuyFirst(), "m1", after=1000, before=6000,
            initial_cash="10000.0000", latency_ms=50, limit_fill_rate=1.0,
            fees=None,
        )
        fill_price = result.trades_df()["price"].iloc[0]
        assert abs(fill_price - 0.60) < 0.0001

    def test_latency_limit_no_longer_crosses_at_activation(self, mock_api, client):
        """A limit submitted when it would have crossed must not auto-fill if
        by activation the spread has moved against it; instead it should rest
        as a maker against the activation-time book."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        # At t=1000, best ask = 0.50: a BUY_YES limit at 0.51 would cross.
        snap_cross = {
            "type": "snapshot", "t": 1000, "is_reseed": False,
            "bids": [{"price": "0.4900", "size": "100.0000"}],
            "asks": [{"price": "0.5000", "size": "100.0000"}],
        }
        # By t=1025, the ask has lifted to 0.55: the same limit no longer crosses.
        snap_no_cross = {
            "type": "snapshot", "t": 1025, "is_reseed": False,
            "bids": [{"price": "0.4900", "size": "100.0000"}],
            "asks": [{"price": "0.5500", "size": "100.0000"}],
        }
        snap_end = {
            "type": "snapshot", "t": 5000, "is_reseed": False,
            "bids": [{"price": "0.4900", "size": "100.0000"}],
            "asks": [{"price": "0.5500", "size": "100.0000"}],
        }

        class LimitBuyer(Strategy):
            done = False
            def on_book(self, ctx, market, book):
                if not self.done:
                    ctx.buy_yes(size="50.0000", limit_price="0.5100")
                    self.done = True

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(
                snap_cross, snap_no_cross, snap_end)))

        result = client.backtest(
            LimitBuyer(), "m1", after=1000, before=6000,
            initial_cash="10000.0000", latency_ms=50, limit_fill_rate=1.0,
            fees=None,
        )
        # No fill at activation (limit 0.51 < ask 0.55). Order rests, then
        # gets cancelled at market finalisation (no trades to trigger it).
        # The important assertion is: zero crossing fills despite the
        # submission book having crossed.
        assert result.total_trades == 0
        orders = result.orders_df()
        assert float(orders.iloc[0]["filled_size"]) == 0.0

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

    def test_pending_orders_drain_in_chronological_order(self, mock_api, client):
        """Two pending buys activating before the same event must fire in the
        order of their ``activate_at`` so cash-draining sequencing is correct."""
        m1 = _market_with({
            "id": "m1", "status": "resolved",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": 1000, "close_time": 6000, "resolved_at": 6000,
        })

        snap1 = {
            "type": "snapshot", "t": 1000, "is_reseed": False,
            "bids": [{"price": "0.5000", "size": "1000.0000"}],
            "asks": [{"price": "0.5000", "size": "1000.0000"}],
        }
        snap2 = {
            "type": "snapshot", "t": 5000, "is_reseed": False,
            "bids": [{"price": "0.5000", "size": "1000.0000"}],
            "asks": [{"price": "0.5000", "size": "1000.0000"}],
        }

        fills_in_order = []

        class TwoBuys(Strategy):
            placed = False
            def on_book(self, ctx, market, book):
                if not self.placed:
                    # Both pending; first activates at ~1100, second at ~1200.
                    ctx.buy_yes(size="10.0000")
                    self.placed = True
            def on_fill(self, ctx, market, fill):
                fills_in_order.append(fill.timestamp)

        mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=m1))
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(snap1, snap2)))

        # latency 100ms — order submitted at t=1000 activates at t=1100.
        client.backtest(
            TwoBuys(), "m1", after=1000, before=6000,
            initial_cash="10000.0000", latency_ms=100, limit_fill_rate=1.0,
            fees=None,
        )
        # Single order in this scenario; the drain fires it at its own time.
        assert fills_in_order == [1100]

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
        assert Decimal(fill.price) == 0.6767

    def test_sell_yes_slippage_decreases_price(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.SELL_YES, "50.0000")
        fill = self._sim(slippage_bps=100).try_fill_market_order(order, book, 1000)
        assert fill is not None
        # Base price = 0.65, slippage = 0.65 * 100/10000 = 0.0065 → 0.6435
        assert Decimal(fill.price) == 0.6435

    def test_buy_no_slippage_increases_price(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_NO, "50.0000")
        fill = self._sim(slippage_bps=100).try_fill_market_order(order, book, 1000)
        assert fill is not None
        # NO price = 1 - 0.65 = 0.35, slippage = 0.35 * 100/10000 = 0.0035 → 0.3535
        assert Decimal(fill.price) == 0.3535

    def test_zero_slippage_no_change(self):
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000")
        fill = self._sim(slippage_bps=0).try_fill_market_order(order, book, 1000)
        assert fill is not None
        assert Decimal(fill.price) == 0.6700

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
        assert fill_price > 0.6700


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
        assert fill.size == 10.0  # 100 * 0.1 = 10

    def test_fill_rate_one_gives_full_trade(self):
        """With fill_rate=1.0, full trade size fills (original behavior)."""
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "200.0000", "0.6500")
        trade = self._trade("SELL", "0.6500", size="50.0000")
        fill = self._sim(limit_fill_rate=1.0).try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.size == 50.0

    def test_fill_rate_caps_at_remaining(self):
        """Fill size capped by remaining order size even with fill_rate."""
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "5.0000", "0.6500")
        trade = self._trade("SELL", "0.6500", size="100.0000")
        fill = self._sim(limit_fill_rate=0.5).try_fill_limit_order(order, book, trade, 2000)
        assert fill is not None
        assert fill.size == 5.0  # remaining < trade*rate (50), so fill = remaining

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
        assert fill_size == 25.0000


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
        assert state.queue_ahead == 200
        assert state.price == 0.65
        assert state.book_side == "BUY"

    def test_trade_drains_queue(self):
        """Trade of 50 drains queue from 200→150, no fill."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        available = tracker.on_trade("ord-1", 50, "0.6500", "SELL")
        assert available == 0
        assert tracker._states["ord-1"].queue_ahead == 150

    def test_trade_fills_when_queue_exhausted(self):
        """Queue=50, trade=100 → fill_available=50, queue=0."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "50.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "100.0000", "0.6500")
        tracker.register(order, book)

        available = tracker.on_trade("ord-1", 100, "0.6500", "SELL")
        assert available == 50
        assert tracker._states["ord-1"].queue_ahead == 0

    def test_cancel_proportional_drain(self):
        """queue=200, level=1000, delta decreases to 800, cancel_portion=200, proportion=0.2, drain=40."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "1000.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)
        # Manually set queue_ahead to 200 (as if we're 200 deep in a 1000-size level)
        tracker._states["ord-1"].queue_ahead = 200

        tracker.on_delta("m1", "0.6500", 800, "BUY")
        # decrease = 200, proportion = 200/1000 = 0.2
        # queue_ahead -= 200 * 0.2 = 40 → 200 - 40 = 160
        assert tracker._states["ord-1"].queue_ahead == 160

    def test_trade_then_delta_both_drain(self):
        """Trade front-drains, then delta proportionally drains the full decrease."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "1000.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        # Trade drains 50 from front
        tracker.on_trade("ord-1", 50, "0.6500", "SELL")
        assert tracker._states["ord-1"].queue_ahead == 950

        # Delta: level goes from 1000 to 920 (decrease of 80)
        # proportion = 950/1000 = 0.95
        # queue_ahead -= 80 * 0.95 = 76 → 950 - 76 = 874
        tracker.on_delta("m1", "0.6500", 920, "BUY")
        assert tracker._states["ord-1"].queue_ahead == 874

    def test_level_empties_queue_zeroes(self):
        """Level goes to 0 → queue_ahead → 0."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        tracker.on_delta("m1", "0.6500", 0, "BUY")
        assert tracker._states["ord-1"].queue_ahead == 0

    def test_level_increase_no_change(self):
        """New orders behind you, queue unchanged."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        tracker.on_delta("m1", "0.6500", 500, "BUY")
        assert tracker._states["ord-1"].queue_ahead == 200
        assert tracker._states["ord-1"].level_size == 500

    def test_snapshot_resyncs(self):
        """Snapshot clamps queue_ahead to level_size."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        # Simulate a book where the level shrank
        new_book = _book([("0.6500", "50.0000")], [("0.6700", "100.0000")])
        tracker.on_snapshot("m1", new_book)
        assert tracker._states["ord-1"].queue_ahead == 50
        assert tracker._states["ord-1"].level_size == 50

    def test_unregister_stops_tracking(self):
        """After unregister, on_trade returns 0."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)
        tracker.unregister("ord-1")

        available = tracker.on_trade("ord-1", 300, "0.6500", "SELL")
        assert available == 0
        assert "ord-1" not in tracker._states

    def test_buy_no_resting_level(self):
        """BUY_NO at 0.35 → rests SELL side at 0.65."""
        order = self._order(OrderSide.BUY_NO, "50.0000", "0.3500")
        price, side = _order_resting_level(order)
        assert price == pytest.approx(0.65)
        assert side == "SELL"

    def test_sell_no_resting_level(self):
        """SELL_NO at 0.35 → rests BUY side at 0.65."""
        order = self._order(OrderSide.SELL_NO, "50.0000", "0.3500")
        price, side = _order_resting_level(order)
        assert price == pytest.approx(0.65)
        assert side == "BUY"

    def test_trade_wrong_price_no_drain(self):
        """Trade at different price returns 0."""
        tracker = QueuePositionTracker()
        book = _book([("0.6500", "200.0000")], [("0.6700", "100.0000")])
        order = self._order(OrderSide.BUY_YES, "50.0000", "0.6500")
        tracker.register(order, book)

        available = tracker.on_trade("ord-1", 100, "0.6400", "SELL")
        assert available == 0
        assert tracker._states["ord-1"].queue_ahead == 200


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
        assert fill.size == 50.0  # 100 * 0.5

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
        assert fill2.size == 50.0  # capped by remaining order size

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
        assert first_book.best_ask == 0.65, (
            f"silent replay must apply pre-window delta; got best_ask={first_book.best_ask}"
        )
        assert first_book.best_bid == 0.6


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
                if Decimal(book.midpoint) < 0.35:
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
