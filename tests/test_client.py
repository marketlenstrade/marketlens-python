import httpx
import pytest
import respx

from conftest import (
    BASE_URL,
    SAMPLE_CANDLE,
    SAMPLE_EVENT,
    SAMPLE_MARKET,
    SAMPLE_ORDERBOOK,
    SAMPLE_BOOK_METRICS,
    SAMPLE_SERIES,
    SAMPLE_TRADE,
)
from marketlens import MarketLens, Market, Event, Series, Trade, Candle, OrderBook, BookMetrics


class TestMarkets:
    def test_list_markets(self, mock_api, client):
        mock_api.get("/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_MARKET],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        markets = client.markets.list(status="active").to_list()
        assert len(markets) == 1
        assert isinstance(markets[0], Market)
        assert markets[0].question == "Will BTC reach 100k?"

    def test_get_market(self, mock_api, client):
        mock_api.get("/markets/abc-123").mock(
            return_value=httpx.Response(200, json=SAMPLE_MARKET)
        )
        market = client.markets.get("abc-123")
        assert isinstance(market, Market)
        assert market.id == "abc-123"
        assert market.outcomes[0].name == "Yes"

    def test_list_trades(self, mock_api, client):
        mock_api.get("/markets/abc-123/trades").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_TRADE],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        trades = client.markets.trades("abc-123").to_list()
        assert len(trades) == 1
        assert isinstance(trades[0], Trade)
        assert trades[0].side == "BUY"

    def test_list_candles(self, mock_api, client):
        mock_api.get("/markets/abc-123/candles").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_CANDLE],
                "meta": {"cursor": None, "has_more": False, "resolution": "1h"},
            })
        )
        candles = client.markets.candles("abc-123", resolution="1h").to_list()
        assert len(candles) == 1
        assert isinstance(candles[0], Candle)
        assert candles[0].trade_count == 47


class TestEvents:
    def test_list_events(self, mock_api, client):
        mock_api.get("/events").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_EVENT],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        events = client.events.list().to_list()
        assert len(events) == 1
        assert isinstance(events[0], Event)

    def test_get_event(self, mock_api, client):
        mock_api.get("/events/evt-1").mock(
            return_value=httpx.Response(200, json=SAMPLE_EVENT)
        )
        event = client.events.get("evt-1")
        assert event.title == "Test Event"

    def test_event_markets(self, mock_api, client):
        mock_api.get("/events/evt-1/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_MARKET],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        markets = client.events.markets("evt-1").to_list()
        assert len(markets) == 1


class TestSeries:
    def test_list_series(self, mock_api, client):
        mock_api.get("/series").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_SERIES],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        series_list = client.series.list().to_list()
        assert len(series_list) == 1
        assert isinstance(series_list[0], Series)

    def test_get_series(self, mock_api, client):
        mock_api.get("/series/btc-daily").mock(
            return_value=httpx.Response(200, json=SAMPLE_SERIES)
        )
        s = client.series.get("btc-daily")
        assert s.is_rolling is True

    def test_series_markets(self, mock_api, client):
        mock_api.get("/series/btc-daily").mock(
            return_value=httpx.Response(200, json=SAMPLE_SERIES)
        )
        mock_api.get("/series/btc-daily/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_MARKET],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        markets = client.series.markets("btc-daily").to_list()
        assert len(markets) == 1


class TestOrderbook:
    def test_get_orderbook(self, mock_api, client):
        mock_api.get("/markets/abc-123/orderbook").mock(
            return_value=httpx.Response(200, json=SAMPLE_ORDERBOOK)
        )
        book = client.orderbook.get("abc-123")
        assert isinstance(book, OrderBook)
        assert book.best_bid == pytest.approx(0.65)
        assert len(book.bids) == 3

    def test_orderbook_metrics(self, mock_api, client):
        mock_api.get("/markets/abc-123/orderbook/metrics").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_BOOK_METRICS],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        metrics = client.orderbook.metrics(
            "abc-123", after=1700000000000, before=1700100000000, resolution="5m"
        ).to_list()
        assert len(metrics) == 1
        assert isinstance(metrics[0], BookMetrics)

    def test_orderbook_history(self, mock_api, client):
        mock_api.get("/markets/abc-123/orderbook/history").mock(
            return_value=httpx.Response(200, json={
                "data": [
                    {
                        "type": "snapshot",
                        "t": 1700000060000,
                        "is_reseed": False,
                        "bids": [{"price": "0.6500", "size": "200.0000"}],
                        "asks": [{"price": "0.6700", "size": "100.0000"}],
                    },
                    {
                        "type": "delta",
                        "t": 1700000061234,
                        "price": "0.6500",
                        "size": "350.0000",
                        "side": "BUY",
                    },
                    {
                        "type": "trade",
                        "t": 1700000062500,
                        "id": "01TRADE1",
                        "price": "0.6700",
                        "size": "100.0000",
                        "side": "BUY",
                    },
                ],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        events = client.orderbook.history(
            "abc-123", after=1700000000000, before=1700100000000
        ).to_list()
        assert len(events) == 3
        assert events[0].type == "snapshot"
        assert events[1].type == "delta"
        assert events[2].type == "trade"
