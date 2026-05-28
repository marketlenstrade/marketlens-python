import httpx
import pytest

from conftest import BASE_URL, SAMPLE_EVENT, SAMPLE_MARKET, SAMPLE_SERIES, SAMPLE_SERIES_NONROLLING
from marketlens.types.event import Event
from marketlens.types.market import Market


def _market_with(overrides):
    return {**SAMPLE_MARKET, **overrides}


def _mock_series_resolve(mock_api):
    """Mock the /series/btc-daily GET used by _resolve()."""
    mock_api.get("/series/btc-daily").mock(
        return_value=httpx.Response(200, json=SAMPLE_SERIES)
    )


def _mock_nonrolling_series_resolve(mock_api):
    """Mock the /series/btc-hit-price GET for a non-rolling series."""
    mock_api.get("/series/btc-hit-price").mock(
        return_value=httpx.Response(200, json=SAMPLE_SERIES_NONROLLING)
    )


SNAPSHOT_1 = {
    "type": "snapshot",
    "t": 1000,
    "is_reseed": False,
    "bids": [{"price": "0.60", "size": "100.0"}],
    "asks": [{"price": "0.70", "size": "100.0"}],
}
DELTA_1 = {
    "type": "delta",
    "t": 1500,
    "price": "0.61",
    "size": "50.0",
    "side": "BUY",
}
SNAPSHOT_2 = {
    "type": "snapshot",
    "t": 5000,
    "is_reseed": False,
    "bids": [{"price": "0.55", "size": "200.0"}],
    "asks": [{"price": "0.65", "size": "200.0"}],
}


def _history_response(*events):
    return {"data": list(events), "meta": {"cursor": None, "has_more": False}}


class TestSeriesWalk:
    def test_series_walk_yields_markets(self, mock_api, client):
        """walk() should yield Market objects in chronological order."""
        _mock_series_resolve(mock_api)
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 2000})
        m2 = _market_with({"id": "m2", "open_time": 2000, "close_time": 3000})
        m3 = _market_with({"id": "m3", "open_time": 3000, "close_time": 4000})

        mock_api.get("/series/btc-daily/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [m1, m2, m3],
                "meta": {"cursor": None, "has_more": False},
            })
        )

        markets = list(client.series.walk("btc-daily"))
        assert len(markets) == 3
        assert all(isinstance(m, Market) for m in markets)
        assert markets[0].id == "m1"
        assert markets[1].id == "m2"
        assert markets[2].id == "m3"

    def test_series_walk_rejects_non_rolling(self, mock_api, client):
        """walk() should raise ValueError for non-rolling series."""
        _mock_nonrolling_series_resolve(mock_api)
        with pytest.raises(ValueError, match="not rolling"):
            list(client.series.walk("btc-hit-price"))

    def test_series_walk_excludes_boundary_touching_markets(self, mock_api, client):
        """walk(after, before) maps to a half-open ``[after, before)`` window:
        a market with ``close_time == after`` or ``open_time == before`` is
        excluded.

        The server's ``/markets`` endpoint exposes inclusive ``close_after`` /
        ``open_before`` params. The SDK translates the user's half-open window
        to the server's inclusive params via ``+1`` / ``-1`` on the integer-ms
        timestamps so the resulting set matches ``/series/{id}/export``.
        """
        _mock_series_resolve(mock_api)

        AFTER = 2_000_000
        BEFORE = 3_000_000
        # The handler will receive close_after=AFTER+1, open_before=BEFORE-1.
        route = mock_api.get("/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [],
                "meta": {"cursor": None, "has_more": False},
            })
        )

        list(client.series.walk("btc-daily", after=AFTER, before=BEFORE))

        assert route.call_count == 1
        params = route.calls[0].request.url.params
        assert params["close_after"] == str(AFTER + 1), (
            f"expected close_after={AFTER + 1} (half-open lower bound), "
            f"got {params['close_after']}"
        )
        assert params["open_before"] == str(BEFORE - 1), (
            f"expected open_before={BEFORE - 1} (half-open upper bound), "
            f"got {params['open_before']}"
        )


class TestSeriesEvents:
    def test_series_events(self, mock_api, client):
        """events() should return Event objects for a series."""
        _mock_nonrolling_series_resolve(mock_api)
        evt = {**SAMPLE_EVENT, "series_id": "btc-hit-price"}
        mock_api.get("/events").mock(
            return_value=httpx.Response(200, json={
                "data": [evt],
                "meta": {"cursor": None, "has_more": False},
            })
        )

        events = client.series.events("btc-hit-price").to_list()
        assert len(events) == 1
        assert isinstance(events[0], Event)
        assert events[0].id == evt["id"]


class TestOrderbookWalk:
    def test_orderbook_walk_yields_tuples(self, mock_api, client):
        """walk() should yield (Market, OrderBook) tuples."""
        _mock_series_resolve(mock_api)
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 2000})

        # Market GET returns 404 so it falls back to series
        mock_api.get("/markets/btc-daily").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"},
            })
        )
        mock_api.get("/series/btc-daily/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [m1],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1))
        )

        results = list(client.orderbook.walk("btc-daily"))
        assert len(results) == 1
        market, book = results[0]
        assert isinstance(market, Market)
        assert market.id == "m1"
        assert book.best_bid == pytest.approx(0.60)

    def test_orderbook_walk_multiple_markets(self, mock_api, client):
        """walk() should cross market boundaries with fresh replay per market."""
        _mock_series_resolve(mock_api)
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 2000})
        m2 = _market_with({"id": "m2", "open_time": 3000, "close_time": 6000})

        mock_api.get("/markets/btc-daily").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"},
            })
        )
        mock_api.get("/series/btc-daily/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [m1, m2],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1, DELTA_1))
        )
        mock_api.get("/markets/m2/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_2))
        )

        results = list(client.orderbook.walk("btc-daily"))
        assert len(results) == 3  # snapshot + delta from m1, snapshot from m2

        # m1 events
        assert results[0][0].id == "m1"
        assert results[1][0].id == "m1"
        # m2 event — fresh replay, different book
        assert results[2][0].id == "m2"
        assert results[2][1].best_bid == pytest.approx(0.55)

    def test_orderbook_walk_params_passthrough(self, mock_api, client):
        """status and other params should be forwarded to series.walk."""
        _mock_series_resolve(mock_api)
        m1 = _market_with({"id": "m1", "status": "resolved",
                           "open_time": 1000, "close_time": 2000})

        mock_api.get("/markets/btc-daily").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"},
            })
        )
        route = mock_api.get("/series/btc-daily/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [m1],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1))
        )

        list(client.orderbook.walk("btc-daily", status="resolved"))
        assert route.call_count == 1

    def test_orderbook_walk_to_dataframe(self, mock_api, client):
        """to_dataframe() should include market_id and winning_outcome columns."""
        _mock_series_resolve(mock_api)
        m1 = _market_with({
            "id": "m1", "open_time": 1000, "close_time": 2000,
            "status": "resolved", "winning_outcome": "Up",
        })

        mock_api.get("/markets/btc-daily").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"},
            })
        )
        mock_api.get("/series/btc-daily/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [m1],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1, DELTA_1))
        )

        df = client.orderbook.walk("btc-daily").to_dataframe()
        assert "market_id" in df.columns
        assert "winning_outcome" in df.columns
        assert list(df["market_id"].unique()) == ["m1"]
        assert list(df["winning_outcome"].unique()) == ["Up"]
        assert "midpoint" in df.columns
        assert "spread_bps" in df.columns
        assert len(df) == 2

    def test_orderbook_walk_to_dataframe_empty(self, mock_api, client):
        """to_dataframe() with no markets returns empty DataFrame."""
        _mock_series_resolve(mock_api)
        mock_api.get("/markets/btc-daily").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"},
            })
        )
        mock_api.get("/series/btc-daily/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [],
                "meta": {"cursor": None, "has_more": False},
            })
        )

        df = client.orderbook.walk("btc-daily").to_dataframe()
        assert df.empty

    def test_orderbook_walk_single_market(self, mock_api, client):
        """walk() with a market ID should replay that single market."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 5000})

        mock_api.get("/markets/m1").mock(
            return_value=httpx.Response(200, json=m1)
        )
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1, DELTA_1))
        )

        results = list(client.orderbook.walk("m1", after=1000, before=5000))
        assert len(results) == 2
        market, book = results[0]
        assert isinstance(market, Market)
        assert market.id == "m1"
        assert book.best_bid == pytest.approx(0.60)

    def test_orderbook_walk_single_market_to_dataframe(self, mock_api, client):
        """walk() with a market ID supports to_dataframe()."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 5000})

        mock_api.get("/markets/m1").mock(
            return_value=httpx.Response(200, json=m1)
        )
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1, DELTA_1))
        )

        df = client.orderbook.walk("m1", after=1000, before=5000).to_dataframe()
        assert len(df) == 2
        assert "market_id" in df.columns
        assert list(df["market_id"].unique()) == ["m1"]

    def test_orderbook_walk_falls_back_to_series(self, mock_api, client):
        """walk() falls back to series when market GET returns 404."""
        m1 = _market_with({"id": "m1", "open_time": 1000, "close_time": 2000})

        # Market GET → 404
        mock_api.get("/markets/btc-daily").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"},
            })
        )
        # Series resolve
        _mock_series_resolve(mock_api)
        mock_api.get("/series/btc-daily/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [m1],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(200, json=_history_response(SNAPSHOT_1))
        )

        results = list(client.orderbook.walk("btc-daily"))
        assert len(results) == 1
        assert results[0][0].id == "m1"
