import pytest

from marketlens import (
    Market,
    Event,
    Series,
    Trade,
    Candle,
    OrderBook,
    PriceLevel,
    BookMetrics,
    SnapshotEvent,
    DeltaEvent,
    TradeEvent,
)
from conftest import (
    SAMPLE_MARKET,
    SAMPLE_EVENT,
    SAMPLE_SERIES,
    SAMPLE_TRADE,
    SAMPLE_CANDLE,
    SAMPLE_ORDERBOOK,
    SAMPLE_BOOK_METRICS,
)


class TestTypesParsing:
    def test_market(self):
        m = Market.model_validate(SAMPLE_MARKET)
        assert m.id == "abc-123"
        assert m.outcomes[0].last_price == pytest.approx(0.65)
        assert m.status == "active"

    def test_event(self):
        e = Event.model_validate(SAMPLE_EVENT)
        assert e.market_count == 3

    def test_series(self):
        s = Series.model_validate(SAMPLE_SERIES)
        assert s.is_rolling is True
        assert s.market_count == 365

    def test_trade(self):
        t = Trade.model_validate(SAMPLE_TRADE)
        assert t.side == "BUY"
        assert t.fee_rate_bps == pytest.approx(50.0)

    def test_candle(self):
        c = Candle.model_validate(SAMPLE_CANDLE)
        assert c.trade_count == 47
        assert c.vwap == pytest.approx(0.6537)

    def test_orderbook(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        assert ob.bid_levels == 3
        assert ob.best_bid == pytest.approx(0.65)
        assert len(ob.asks) == 3

    def test_book_metrics(self):
        bm = BookMetrics.model_validate(SAMPLE_BOOK_METRICS)
        assert bm.spread == pytest.approx(0.02)

    def test_snapshot_event(self):
        raw = {
            "type": "snapshot",
            "t": 1700000060000,
            "is_reseed": False,
            "bids": [{"price": 0.65, "size": 200.0}],
            "asks": [{"price": 0.67, "size": 100.0}],
        }
        e = SnapshotEvent.model_validate(raw)
        assert e.type == "snapshot"
        assert len(e.bids) == 1

    def test_delta_event(self):
        raw = {"type": "delta", "t": 1700000061234, "price": 0.65, "size": 350.0, "side": "BUY"}
        e = DeltaEvent.model_validate(raw)
        assert e.side == "BUY"

    def test_trade_event(self):
        raw = {"type": "trade", "t": 1700000062500, "id": "01XYZ", "price": 0.67, "size": 100.0, "side": "BUY"}
        e = TradeEvent.model_validate(raw)
        assert e.id == "01XYZ"


class TestNoneDefaults:
    def test_market_none_volume_becomes_zero(self):
        raw = dict(SAMPLE_MARKET)
        raw["volume"] = None
        raw["liquidity"] = None
        m = Market.model_validate(raw)
        assert m.volume == 0.0
        assert m.liquidity == 0.0

    def test_outcome_none_last_price_becomes_half(self):
        """Never-traded outcomes fall back to the 0.5 neutral prior."""
        raw = dict(SAMPLE_MARKET)
        raw["outcomes"] = [
            {"name": "Yes", "index": 0, "platform_token_id": "tok1", "last_price": None},
        ]
        m = Market.model_validate(raw)
        assert m.outcomes[0].last_price == 0.5

    def test_candle_none_vwap_becomes_zero(self):
        raw = dict(SAMPLE_CANDLE)
        raw["vwap"] = None
        c = Candle.model_validate(raw)
        assert c.vwap == 0.0

    def test_trade_none_fee_rate_becomes_zero(self):
        raw = dict(SAMPLE_TRADE)
        raw["fee_rate_bps"] = None
        t = Trade.model_validate(raw)
        assert t.fee_rate_bps == 0.0

    def test_orderbook_empty_side_metrics_default(self):
        """Price-like fields fall back to the 0.5 neutral prior; size-like
        fields fall back to 0.0."""
        raw = dict(SAMPLE_ORDERBOOK)
        raw["bids"] = []
        raw["best_bid"] = None
        raw["spread"] = None
        raw["midpoint"] = None
        raw["bid_depth"] = None
        raw["bid_levels"] = 0
        ob = OrderBook.model_validate(raw)
        assert ob.best_bid == 0.5
        assert ob.midpoint == 0.5
        assert ob.spread == 0.0
        assert ob.bid_depth == 0.0


class TestOrderBookHelpers:
    def test_impact_buy(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        # Buy 100 at 0.67 (fills entire first ask level)
        avg = ob.impact("BUY", 100.0)
        assert avg == pytest.approx(0.67)

    def test_impact_buy_multi_level(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        # Buy 350: 100 @ 0.67 + 250 @ 0.68
        avg = ob.impact("BUY", 350.0)
        assert avg is not None
        assert avg > 0.67

    def test_impact_sell(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        # Sell 200 at best bid 0.65
        avg = ob.impact("SELL", 200.0)
        assert avg == pytest.approx(0.65)

    def test_impact_insufficient_liquidity(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        # Try to buy more than total ask depth (750)
        avg = ob.impact("BUY", 1000.0)
        # Should still return an avg (partial fill)
        assert avg is not None

    def test_depth_within(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        # All levels within 0.05 of midpoint 0.66
        bid_d, ask_d = ob.depth_within(0.05)
        assert bid_d == pytest.approx(850.0)
        assert ask_d == pytest.approx(750.0)

    def test_depth_within_narrow(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        # Only levels within 0.01 of mid: bid 0.65, ask 0.67
        bid_d, ask_d = ob.depth_within(0.01)
        assert bid_d == pytest.approx(200.0)
        assert ask_d == pytest.approx(100.0)

    def test_slippage(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        # Midpoint 0.66, buy 100 fills at 0.67 exactly
        slip = ob.slippage("BUY", 100.0)
        assert slip == pytest.approx(0.01)

    def test_imbalance(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        # bid_depth=850, ask_depth=750 → 100/1600 = 0.0625
        imb = ob.imbalance()
        assert imb is not None
        assert abs(imb - 0.0625) < 0.001

    def test_imbalance_empty_book(self):
        ob = OrderBook(
            market_id="x", platform="p", as_of=0,
            bids=[], asks=[],
            bid_depth=0.0, ask_depth=0.0,
            bid_levels=0, ask_levels=0,
        )
        assert ob.imbalance() is None

    def test_weighted_midpoint_single_level(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        wmid = ob.weighted_midpoint(n=1)
        assert wmid is not None
        # Should sit between the two best levels.
        assert 0.65 < wmid < 0.67

    def test_weighted_midpoint_empty_side(self):
        ob = OrderBook(
            market_id="x", platform="p", as_of=0,
            bids=[], asks=[PriceLevel(price=0.5, size=100.0)],
            bid_levels=0, ask_levels=1,
        )
        assert ob.weighted_midpoint() is None

    def test_microprice(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        mp = ob.microprice()
        wmid = ob.weighted_midpoint(1)
        assert mp is not None
        assert mp == wmid

    def test_spread_bps(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        # spread=0.02, mid=0.66 → 0.02/0.66*10000 ≈ 303.03 bps
        bps = ob.spread_bps()
        assert bps is not None
        assert abs(bps - 303.03) < 1.0

    def test_spread_bps_no_spread(self):
        ob = OrderBook(
            market_id="x", platform="p", as_of=0,
            bids=[], asks=[],
            bid_levels=0, ask_levels=0,
        )
        assert ob.spread_bps() is None

    def test_imbalance_with_levels(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        # Top-1: bid size 200, ask size 100 → (200-100)/(200+100) ≈ 0.333
        imb = ob.imbalance(levels=1)
        assert imb is not None
        assert abs(imb - 0.3333) < 0.01

    def test_imbalance_levels_none_is_total(self):
        ob = OrderBook.model_validate(SAMPLE_ORDERBOOK)
        assert ob.imbalance() == ob.imbalance(levels=None)
