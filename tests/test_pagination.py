import httpx

from conftest import BASE_URL, SAMPLE_MARKET, SAMPLE_CANDLE, SAMPLE_TRADE, SAMPLE_BOOK_METRICS
from marketlens import MarketLens, Market


class TestPagination:
    def test_auto_pagination(self, mock_api, client):
        """Verify that the iterator fetches multiple pages transparently."""
        market_a = {**SAMPLE_MARKET, "id": "page1-market"}
        market_b = {**SAMPLE_MARKET, "id": "page2-market"}

        route = mock_api.get("/markets").mock(
            side_effect=[
                httpx.Response(200, json={
                    "data": [market_a],
                    "meta": {"cursor": "cursor_page2", "has_more": True},
                }),
                httpx.Response(200, json={
                    "data": [market_b],
                    "meta": {"cursor": None, "has_more": False},
                }),
            ]
        )

        markets = list(client.markets.list())
        assert len(markets) == 2
        assert markets[0].id == "page1-market"
        assert markets[1].id == "page2-market"
        assert route.call_count == 2

    def test_first_page(self, mock_api, client):
        mock_api.get("/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_MARKET, SAMPLE_MARKET],
                "meta": {"cursor": "next", "has_more": True},
            })
        )
        page = client.markets.list().first_page()
        assert len(page) == 2
        assert isinstance(page[0], Market)

    def test_empty_response(self, mock_api, client):
        mock_api.get("/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        markets = client.markets.list().to_list()
        assert markets == []

    def test_opaque_cursor_not_coerced_to_timestamp(self, mock_api, client):
        """Cursor strings (server-issued opaque tokens) must pass through the
        param prep layer untouched. Regression guard: an earlier change made
        ``_prepare_params`` route every value through a strict timestamp
        coercer, which crashed on base64 cursors. Coercion now lives at the
        resource boundary for known timestamp keys only."""
        market_a = {**SAMPLE_MARKET, "id": "page1-market"}
        market_b = {**SAMPLE_MARKET, "id": "page2-market"}
        opaque_cursor = (
            "eyJfdHMiOjE3Nzg0MjI1NDQ0MjMsImFjYSI6MTc3ODQyMTU5NDQwNiwi"
            "ZWlkIjoiMDAxOWUxMjMy"  # contains "Z" — used to crash ISO parser
        )
        route = mock_api.get("/markets").mock(
            side_effect=[
                httpx.Response(200, json={
                    "data": [market_a],
                    "meta": {"cursor": opaque_cursor, "has_more": True},
                }),
                httpx.Response(200, json={
                    "data": [market_b],
                    "meta": {"cursor": None, "has_more": False},
                }),
            ]
        )
        markets = list(client.markets.list())
        assert [m.id for m in markets] == ["page1-market", "page2-market"]
        assert route.call_count == 2
        # Page-2 request must echo the cursor verbatim.
        page2_url = str(route.calls[1].request.url)
        assert f"cursor={opaque_cursor}" in page2_url


class TestSmartDataFrame:
    """Verify that to_dataframe() produces properly typed columns."""

    def test_candles_dataframe(self, mock_api, client):
        mock_api.get("/markets/abc-123/candles").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_CANDLE],
                "meta": {"cursor": None, "has_more": False, "resolution": "1h"},
            })
        )
        df = client.markets.candles("abc-123", resolution="1h").to_dataframe()
        assert len(df) == 1
        # Numeric string fields should be float
        assert df["open"].dtype == float
        assert df["high"].dtype == float
        assert df["volume"].dtype == float
        # Index should be datetime
        assert df.index.name == "open_time"
        assert str(df.index.dtype).startswith("datetime64")

    def test_trades_dataframe(self, mock_api, client):
        mock_api.get("/markets/abc-123/trades").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_TRADE],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        df = client.markets.trades("abc-123").to_dataframe()
        assert len(df) == 1
        assert df["price"].dtype == float
        assert df["size"].dtype == float
        assert df.index.name == "platform_timestamp"
        assert str(df.index.dtype).startswith("datetime64")

    def test_book_metrics_dataframe(self, mock_api, client):
        mock_api.get("/markets/abc-123/orderbook/metrics").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_BOOK_METRICS],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        df = client.orderbook.metrics(
            "abc-123", after=1700000000000, before=1700100000000, resolution="5m"
        ).to_dataframe()
        assert len(df) == 1
        assert df["spread"].dtype == float
        assert df["midpoint"].dtype == float
        assert df.index.name == "t"
        assert str(df.index.dtype).startswith("datetime64")

    def test_markets_dataframe(self, mock_api, client):
        mock_api.get("/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [SAMPLE_MARKET],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        df = client.markets.list().to_dataframe()
        assert len(df) == 1
        # Markets have volume as numeric string → should be float
        assert df["volume"].dtype == float
        # Timestamps should be datetime
        assert str(df["open_time"].dtype).startswith("datetime64")
        # Nested outcomes should be excluded
        assert "outcomes" not in df.columns

    def test_empty_dataframe(self, mock_api, client):
        mock_api.get("/markets").mock(
            return_value=httpx.Response(200, json={
                "data": [],
                "meta": {"cursor": None, "has_more": False},
            })
        )
        df = client.markets.list().to_dataframe()
        assert len(df) == 0
