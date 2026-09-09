import httpx
import pytest

from marketlens import (
    AsyncMarketLens,
    MarketLens,
    AuthenticationError,
    DailyBudgetExceededError,
    NotFoundError,
    InvalidParameterError,
    RateLimitError,
    RequestUnitsExceededError,
    RowLimitExceededError,
    StoreUnavailableError,
)
from marketlens import TimeoutError as MarketlensTimeoutError

from conftest import BASE_URL


class TestErrorMapping:
    def test_401_raises_auth_error(self, mock_api, client):
        mock_api.get("/markets/x").mock(
            return_value=httpx.Response(401, json={
                "error": {"code": "UNAUTHORIZED", "message": "Invalid API key", "status": 401}
            })
        )
        with pytest.raises(AuthenticationError) as exc_info:
            client.markets.get("x")
        assert exc_info.value.status_code == 401

    def test_404_raises_not_found(self, mock_api, client):
        mock_api.get("/markets/missing").mock(
            return_value=httpx.Response(404, json={
                "error": {"code": "MARKET_NOT_FOUND", "message": "Not found", "status": 404}
            })
        )
        with pytest.raises(NotFoundError):
            client.markets.get("missing")

    def test_400_raises_invalid_param(self, mock_api, client):
        mock_api.get("/markets").mock(
            return_value=httpx.Response(400, json={
                "error": {"code": "INVALID_PARAMETER", "message": "Bad param", "status": 400}
            })
        )
        with pytest.raises(InvalidParameterError):
            client.markets.list().to_list()

    def test_429_raises_rate_limit(self, mock_api, client):
        mock_api.get("/markets").mock(
            return_value=httpx.Response(
                429,
                json={"error": {"code": "RATE_LIMITED", "message": "Too many requests", "status": 429}},
                headers={"Retry-After": "5"},
            )
        )
        with pytest.raises(RateLimitError) as exc_info:
            client.markets.list().to_list()
        assert exc_info.value.retry_after == 5


_EMPTY_PAGE = {"data": [], "meta": {"cursor": None, "has_more": False}}


class TestStoreUnavailable503:
    """The history store timing out is a 503 STORE_UNAVAILABLE with
    Retry-After: retried like any 5xx, but sleeping the server's hint rather
    than the 1s/2s ladder, and surfacing as its own exception."""

    def test_retried_with_retry_after_then_typed(self, mock_api, client, monkeypatch):
        import marketlens._base as base
        sleeps: list[float] = []
        monkeypatch.setattr(base.time, "sleep", sleeps.append)
        route = mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=httpx.Response(
                503,
                json={"error": {"code": "STORE_UNAVAILABLE",
                                "message": "The history store did not respond in time.",
                                "status": 503}},
                headers={"Retry-After": "5"},
            )
        )
        with pytest.raises(StoreUnavailableError) as exc_info:
            client.orderbook.history("m1", after=1, before=2).first_page()
        assert route.call_count == 1 + client._http.max_retries
        assert sleeps == [5, 5]
        assert exc_info.value.retry_after == 5
        assert exc_info.value.status_code == 503


def _budget_429(code: str) -> httpx.Response:
    return httpx.Response(
        429,
        json={"error": {"code": code, "message": "budget exhausted", "status": 429}},
        headers={"Retry-After": "600"},
    )


class TestBudget429Codes:
    """The three budget codes map to their own exceptions and are never
    retried: their budgets reset at wall-clock boundaries, so an in-process
    retry can only burn attempts."""

    @pytest.mark.parametrize("code,exc_cls", [
        ("DAILY_BUDGET_EXCEEDED", DailyBudgetExceededError),
        ("ROW_LIMIT_EXCEEDED", RowLimitExceededError),
        ("UNIT_LIMIT_EXCEEDED", RequestUnitsExceededError),
    ])
    def test_code_maps_and_never_retries(self, mock_api, client, code, exc_cls):
        route = mock_api.get("/markets").mock(return_value=_budget_429(code))
        with pytest.raises(exc_cls) as exc_info:
            client.markets.list().to_list()
        assert exc_info.value.retry_after == 600
        assert exc_info.value.code == code
        assert route.call_count == 1  # no retry

    def test_plain_429_still_retries(self, mock_api, client):
        route = mock_api.get("/markets")
        route.side_effect = [
            _budget_429("RATE_LIMITED"),
            httpx.Response(200, json=_EMPTY_PAGE),
        ]
        # Patch out the backoff sleep so the retry is instant.
        import marketlens._base as base
        orig_sleep = base.time.sleep
        base.time.sleep = lambda *_: None
        try:
            assert client.markets.list().to_list() == []
        finally:
            base.time.sleep = orig_sleep
        assert route.call_count == 2


class TestTimeoutRetryPolicy:
    """Read timeouts fail fast (the server is still running the query, and a
    retry re-runs it at full cost with the same deadline); connect timeouts
    never reached the server, so they stay retryable."""

    def test_read_timeout_is_not_retried(self, mock_api, client):
        route = mock_api.get("/markets").mock(side_effect=httpx.ReadTimeout("read timed out"))
        with pytest.raises(MarketlensTimeoutError):
            client.markets.list().to_list()
        assert route.call_count == 1

    def test_connect_timeout_is_retried(self, mock_api, client):
        route = mock_api.get("/markets")
        route.side_effect = [
            httpx.ConnectTimeout("connect timed out"),
            httpx.Response(200, json=_EMPTY_PAGE),
        ]
        assert client.markets.list().to_list() == []
        assert route.call_count == 2

    async def test_read_timeout_is_not_retried_async(self, mock_api):
        route = mock_api.get("/markets").mock(side_effect=httpx.ReadTimeout("read timed out"))
        async with AsyncMarketLens(api_key="mk_test_key", base_url=BASE_URL) as ac:
            with pytest.raises(MarketlensTimeoutError):
                await ac.markets.list().to_list()
        assert route.call_count == 1

    async def test_connect_timeout_is_retried_async(self, mock_api):
        route = mock_api.get("/markets")
        route.side_effect = [
            httpx.ConnectTimeout("connect timed out"),
            httpx.Response(200, json=_EMPTY_PAGE),
        ]
        async with AsyncMarketLens(api_key="mk_test_key", base_url=BASE_URL) as ac:
            assert await ac.markets.list().to_list() == []
        assert route.call_count == 2


class TestStreamingDownloadRetry:
    """The streaming (progress-reporting) download path must see the 429
    error code before deciding to retry: budget walls are non-retryable and
    their Retry-After can be a month."""

    class _Reporter:
        def download_started(self, *a): pass
        def download_progress(self, *a): pass
        def download_finished(self, *a): pass

    def test_streamed_budget_429_raises_immediately(self, mock_api, client, tmp_path):
        route = mock_api.get("/reference/trades/export").mock(
            return_value=httpx.Response(
                429,
                json={"error": {
                    "code": "DAILY_BUDGET_EXCEEDED",
                    "message": "Daily data row budget exhausted",
                    "status": 429,
                }},
                # If the code were invisible (the pre-1.7.1 bug), the loop
                # would honor this and the test would hang for a day.
                headers={"Retry-After": "86400"},
            )
        )
        with pytest.raises(DailyBudgetExceededError):
            client._http.download(
                "/reference/trades/export", tmp_path / "x.parquet",
                reporter=self._Reporter(),
            )
        assert route.call_count == 1  # never retried

    def test_streamed_plain_429_still_retries(self, mock_api, client, tmp_path, monkeypatch):
        import marketlens._base as base
        sleeps: list[float] = []
        monkeypatch.setattr(base.time, "sleep", sleeps.append)
        route = mock_api.get("/reference/trades/export").mock(
            return_value=httpx.Response(
                429,
                json={"error": {"code": "RATE_LIMITED", "message": "slow down", "status": 429}},
                headers={"Retry-After": "7"},
            )
        )
        with pytest.raises(RateLimitError):
            client._http.download(
                "/reference/trades/export", tmp_path / "x.parquet",
                reporter=self._Reporter(),
            )
        assert route.call_count == 1 + client._http.max_retries
        assert sleeps and all(s >= 7 for s in sleeps)  # Retry-After honored
