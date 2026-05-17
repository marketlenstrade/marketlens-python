"""Tests for the 1.2.0 exports flow.

Per-market: 302 redirect from the API → presigned bucket URL we fetch
unauthenticated. Per-series: JSON manifest with ready/pending/failed
buckets and one presigned URL per ready market.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest
import respx

from conftest import BASE_URL
from marketlens import (
    AsyncMarketLens,
    ExportNotReadyError,
    MarketLens,
    NotFoundError,
    RateLimitError,
    SeriesDownloadResult,
)


BUCKET_BASE = "https://bucket.example.com/marketlens"


def _market_404(mock_api, market_id: str = "m1") -> None:
    """Short-circuit the optional ``self._markets.get(...)`` lookup so the
    underlying-reference download is skipped."""
    mock_api.get(f"/markets/{market_id}").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "MARKET_NOT_FOUND", "message": "x"}},
        )
    )


def _series_404(mock_api, series_id: str) -> None:
    """Short-circuit the optional underlying walk in ``download_series``.

    ``self._series.walk(...)`` hits ``/series/{id}`` first; a 404 there raises
    NotFoundError which is swallowed by ``download_series``'s ``except`` clause.
    """
    mock_api.get(f"/series/{series_id}").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "SERIES_NOT_FOUND", "message": "x"}},
        )
    )


# ── Per-market download ────────────────────────────────────────────


class TestMarketDownload:
    def test_follows_302_to_presigned_url(self, mock_api, client, tmp_path):
        body = b"PAR1" + b"x" * 1024
        bucket_url = f"{BUCKET_BASE}/history/m1.parquet"
        mock_api.get("/markets/m1/export").mock(
            return_value=httpx.Response(
                302,
                headers={"Location": bucket_url, "X-Export-Events": "100"},
            )
        )
        mock_api.get(bucket_url).mock(return_value=httpx.Response(200, content=body))
        _market_404(mock_api)

        out = client.exports.download(
            "m1", data_dir=str(tmp_path), progress=False, coalesce=False,
        )

        assert (out / "history-m1.parquet").read_bytes() == body
        assert not list(tmp_path.glob("*.part"))

    def test_coalesce_default_compact(self, mock_api, client, tmp_path):
        bucket_url = f"{BUCKET_BASE}/history/m1-compact.parquet"
        mock_api.get("/markets/m1/export").mock(
            return_value=httpx.Response(302, headers={"Location": bucket_url})
        )
        mock_api.get(bucket_url).mock(return_value=httpx.Response(200, content=b"OK"))
        _market_404(mock_api)

        out = client.exports.download("m1", data_dir=str(tmp_path), progress=False)
        assert (out / "history-m1-compact.parquet").exists()

        export_calls = [
            c for c in mock_api.calls
            if c.request.url.path == "/v1/markets/m1/export"
        ]
        assert export_calls and "coalesce=true" in str(export_calls[0].request.url)

    def test_coalesce_false_writes_full(self, mock_api, client, tmp_path):
        bucket_url = f"{BUCKET_BASE}/history/m1.parquet"
        mock_api.get("/markets/m1/export").mock(
            return_value=httpx.Response(302, headers={"Location": bucket_url})
        )
        mock_api.get(bucket_url).mock(return_value=httpx.Response(200, content=b"OK"))
        _market_404(mock_api)

        out = client.exports.download(
            "m1", data_dir=str(tmp_path), progress=False, coalesce=False,
        )
        assert (out / "history-m1.parquet").exists()

        export_calls = [
            c for c in mock_api.calls
            if c.request.url.path == "/v1/markets/m1/export"
        ]
        assert export_calls and "coalesce" not in str(export_calls[0].request.url)

    def test_404_raises_not_found(self, mock_api, client, tmp_path):
        mock_api.get("/markets/missing/export").mock(
            return_value=httpx.Response(
                404, json={"error": {"code": "MARKET_NOT_FOUND", "message": "Market missing not found"}},
            )
        )
        with pytest.raises(NotFoundError):
            client.exports.download("missing", data_dir=str(tmp_path), progress=False)

    def test_409_raises_export_not_ready_with_fields(self, mock_api, client, tmp_path):
        mock_api.get("/markets/m1/export").mock(
            return_value=httpx.Response(
                409,
                json={"error": {
                    "code": "EXPORT_NOT_READY",
                    "message": "Export not ready (status=pending): worker crashed at step 3",
                }},
            )
        )
        with pytest.raises(ExportNotReadyError) as ei:
            client.exports.download("m1", data_dir=str(tmp_path), progress=False)
        assert ei.value.code == "EXPORT_NOT_READY"
        assert ei.value.export_status == "pending"
        assert ei.value.last_error == "worker crashed at step 3"

    def test_409_without_last_error(self, mock_api, client, tmp_path):
        mock_api.get("/markets/m1/export").mock(
            return_value=httpx.Response(
                409,
                json={"error": {
                    "code": "EXPORT_NOT_READY",
                    "message": "Export not ready (status=in_progress)",
                }},
            )
        )
        with pytest.raises(ExportNotReadyError) as ei:
            client.exports.download("m1", data_dir=str(tmp_path), progress=False)
        assert ei.value.export_status == "in_progress"
        assert ei.value.last_error is None

    def test_429_raises_rate_limit_with_retry_after(self, mock_api, tmp_path):
        # Use a no-retry client so the test doesn't sleep through 1+2 retries.
        client = MarketLens(api_key="mk_test_key", base_url=BASE_URL, max_retries=0)
        try:
            mock_api.get("/markets/m1/export").mock(
                return_value=httpx.Response(
                    429,
                    headers={"Retry-After": "60"},
                    json={"error": {"code": "RATE_LIMITED", "message": "Slow down"}},
                )
            )
            with pytest.raises(RateLimitError) as ei:
                client.exports.download("m1", data_dir=str(tmp_path), progress=False)
            assert ei.value.retry_after == 60
        finally:
            client.close()

    def test_skips_when_file_exists(self, mock_api, client, tmp_path):
        (tmp_path / "history-m1-compact.parquet").write_bytes(b"PRE-EXISTING")
        _market_404(mock_api)

        out = client.exports.download("m1", data_dir=str(tmp_path), progress=False)

        # Bytes untouched and no /export call was made.
        assert (out / "history-m1-compact.parquet").read_bytes() == b"PRE-EXISTING"
        export_calls = [
            c for c in mock_api.calls
            if c.request.url.path.endswith("/export")
        ]
        assert export_calls == []

    def test_presigned_fetch_does_not_send_authorization(self, mock_api, client, tmp_path):
        bucket_url = f"{BUCKET_BASE}/history/m1-compact.parquet"
        mock_api.get("/markets/m1/export").mock(
            return_value=httpx.Response(302, headers={"Location": bucket_url})
        )
        bucket_route = mock_api.get(bucket_url).mock(
            return_value=httpx.Response(200, content=b"OK")
        )
        _market_404(mock_api)

        client.exports.download("m1", data_dir=str(tmp_path), progress=False)

        bucket_calls = bucket_route.calls
        assert len(bucket_calls) == 1
        assert "authorization" not in {h.lower() for h in bucket_calls[0].request.headers.keys()}


# ── Per-series download ────────────────────────────────────────────


class TestSeriesDownload:
    def _manifest(self, ready_ids: list[str], pending: list[dict] = None,
                  failed: list[dict] = None, events: int = 0,
                  with_url: bool = True) -> dict:
        return {
            "ready": [
                {"market_id": mid,
                 "url": f"{BUCKET_BASE}/history/{mid}-compact.parquet" if with_url else "",
                 "events": 100}
                for mid in ready_ids
            ],
            "pending": pending or [],
            "failed": failed or [],
            "events_charged": events or 100 * len(ready_ids),
        }

    def test_parses_json_and_downloads_ready(self, mock_api, client, tmp_path):
        manifest = self._manifest(
            ready_ids=["m1", "m2"],
            pending=[{"market_id": "m3", "status": "pending"}],
            failed=[{"market_id": "m4", "error": "boom"}],
            events=12345,
        )
        mock_api.get("/series/btc-daily/export").mock(
            return_value=httpx.Response(200, json=manifest)
        )
        mock_api.get(f"{BUCKET_BASE}/history/m1-compact.parquet").mock(
            return_value=httpx.Response(200, content=b"PAR1-m1")
        )
        mock_api.get(f"{BUCKET_BASE}/history/m2-compact.parquet").mock(
            return_value=httpx.Response(200, content=b"PAR1-m2")
        )
        _series_404(mock_api, "btc-daily")

        result = client.exports.download_series(
            "btc-daily", data_dir=str(tmp_path), progress=False,
        )

        assert isinstance(result, SeriesDownloadResult)
        assert sorted(result.ready) == ["m1", "m2"]
        assert len(result.pending) == 1 and result.pending[0].market_id == "m3"
        assert len(result.failed) == 1 and result.failed[0].market_id == "m4"
        assert result.events_charged == 12345
        assert (tmp_path / "history-m1-compact.parquet").read_bytes() == b"PAR1-m1"
        assert (tmp_path / "history-m2-compact.parquet").read_bytes() == b"PAR1-m2"
        assert not list(tmp_path.glob("*.part"))

    def test_result_is_pathlike(self, mock_api, client, tmp_path):
        mock_api.get("/series/btc-daily/export").mock(
            return_value=httpx.Response(200, json=self._manifest([]))
        )
        _series_404(mock_api, "btc-daily")

        result = client.exports.download_series(
            "btc-daily", data_dir=str(tmp_path), progress=False,
        )
        assert os.fspath(result) == str(tmp_path)
        assert Path(result) == tmp_path

    def test_concurrency_downloads_all_ready(self, mock_api, client, tmp_path):
        ready = [f"m{i}" for i in range(4)]
        mock_api.get("/series/btc-daily/export").mock(
            return_value=httpx.Response(200, json=self._manifest(ready))
        )
        for mid in ready:
            mock_api.get(f"{BUCKET_BASE}/history/{mid}-compact.parquet").mock(
                return_value=httpx.Response(200, content=f"PAR1-{mid}".encode())
            )
        _series_404(mock_api, "btc-daily")

        result = client.exports.download_series(
            "btc-daily", data_dir=str(tmp_path), progress=False, concurrency=4,
        )
        assert sorted(result.ready) == sorted(ready)
        for mid in ready:
            assert (tmp_path / f"history-{mid}-compact.parquet").exists()

    def test_skips_existing_files(self, mock_api, client, tmp_path):
        ready = ["m1", "m2"]
        (tmp_path / "history-m1-compact.parquet").write_bytes(b"OLD")
        mock_api.get("/series/btc-daily/export").mock(
            return_value=httpx.Response(200, json=self._manifest(ready))
        )
        # Only m2 should be fetched. m1's bucket URL is deliberately NOT
        # registered — if the SDK tried to fetch it, respx would raise.
        m2_route = mock_api.get(f"{BUCKET_BASE}/history/m2-compact.parquet").mock(
            return_value=httpx.Response(200, content=b"PAR1-m2")
        )
        _series_404(mock_api, "btc-daily")

        result = client.exports.download_series(
            "btc-daily", data_dir=str(tmp_path), progress=False,
        )

        assert (tmp_path / "history-m1-compact.parquet").read_bytes() == b"OLD"
        assert (tmp_path / "history-m2-compact.parquet").read_bytes() == b"PAR1-m2"
        assert sorted(result.ready) == ["m1", "m2"]
        assert m2_route.call_count == 1

    def test_all_pending(self, mock_api, client, tmp_path):
        manifest = {
            "ready": [],
            "pending": [
                {"market_id": "m1", "status": "pending"},
                {"market_id": "m2", "status": "in_progress"},
            ],
            "failed": [],
            "events_charged": 0,
        }
        mock_api.get("/series/btc-daily/export").mock(
            return_value=httpx.Response(200, json=manifest)
        )
        _series_404(mock_api, "btc-daily")

        result = client.exports.download_series(
            "btc-daily", data_dir=str(tmp_path), progress=False,
        )
        assert result.ready == []
        assert len(result.pending) == 2
        assert result.events_charged == 0
        assert not list(tmp_path.glob("*.parquet"))

    def test_404_unknown_series(self, mock_api, client, tmp_path):
        mock_api.get("/series/unknown/export").mock(
            return_value=httpx.Response(
                404, json={"error": {"code": "SERIES_NOT_FOUND", "message": "x"}},
            )
        )
        with pytest.raises(NotFoundError):
            client.exports.download_series("unknown", data_dir=str(tmp_path), progress=False)

    def test_404_data_not_available(self, mock_api, client, tmp_path):
        mock_api.get("/series/empty-window/export").mock(
            return_value=httpx.Response(
                404, json={"error": {"code": "DATA_NOT_AVAILABLE", "message": "no markets"}},
            )
        )
        with pytest.raises(NotFoundError):
            client.exports.download_series(
                "empty-window",
                after="2026-04-12T00:00:00Z", before="2026-04-11T00:00:00Z",
                data_dir=str(tmp_path), progress=False,
            )


# ── Async mirrors ──────────────────────────────────────────────────


class TestAsyncExports:
    @pytest.fixture
    async def aclient(self):
        c = AsyncMarketLens(api_key="mk_test_key", base_url=BASE_URL)
        yield c
        await c.close()

    async def test_market_download_follows_302(self, mock_api, aclient, tmp_path):
        bucket_url = f"{BUCKET_BASE}/history/m1-compact.parquet"
        mock_api.get("/markets/m1/export").mock(
            return_value=httpx.Response(302, headers={"Location": bucket_url})
        )
        mock_api.get(bucket_url).mock(return_value=httpx.Response(200, content=b"PAR1"))
        _market_404(mock_api)

        out = await aclient.exports.download("m1", data_dir=str(tmp_path), progress=False)
        assert (out / "history-m1-compact.parquet").read_bytes() == b"PAR1"

    async def test_market_download_409_raises_export_not_ready(self, mock_api, aclient, tmp_path):
        mock_api.get("/markets/m1/export").mock(
            return_value=httpx.Response(
                409,
                json={"error": {
                    "code": "EXPORT_NOT_READY",
                    "message": "Export not ready (status=failed): too many retries",
                }},
            )
        )
        with pytest.raises(ExportNotReadyError) as ei:
            await aclient.exports.download("m1", data_dir=str(tmp_path), progress=False)
        assert ei.value.export_status == "failed"
        assert ei.value.last_error == "too many retries"

    async def test_series_download_parses_json(self, mock_api, aclient, tmp_path):
        manifest = {
            "ready": [
                {"market_id": "m1",
                 "url": f"{BUCKET_BASE}/history/m1-compact.parquet",
                 "events": 100},
            ],
            "pending": [{"market_id": "m2", "status": "pending"}],
            "failed": [],
            "events_charged": 100,
        }
        mock_api.get("/series/s1/export").mock(
            return_value=httpx.Response(200, json=manifest)
        )
        mock_api.get(f"{BUCKET_BASE}/history/m1-compact.parquet").mock(
            return_value=httpx.Response(200, content=b"PAR1-m1")
        )
        _series_404(mock_api, "s1")

        result = await aclient.exports.download_series(
            "s1", data_dir=str(tmp_path), progress=False,
        )
        assert result.ready == ["m1"]
        assert len(result.pending) == 1
        assert (tmp_path / "history-m1-compact.parquet").read_bytes() == b"PAR1-m1"

    async def test_series_download_concurrency(self, mock_api, aclient, tmp_path):
        ready = [f"m{i}" for i in range(4)]
        manifest = {
            "ready": [
                {"market_id": mid,
                 "url": f"{BUCKET_BASE}/history/{mid}-compact.parquet",
                 "events": 100}
                for mid in ready
            ],
            "pending": [], "failed": [], "events_charged": 400,
        }
        mock_api.get("/series/s1/export").mock(
            return_value=httpx.Response(200, json=manifest)
        )
        for mid in ready:
            mock_api.get(f"{BUCKET_BASE}/history/{mid}-compact.parquet").mock(
                return_value=httpx.Response(200, content=f"P-{mid}".encode())
            )
        _series_404(mock_api, "s1")

        result = await aclient.exports.download_series(
            "s1", data_dir=str(tmp_path), progress=False, concurrency=4,
        )
        assert sorted(result.ready) == sorted(ready)
        for mid in ready:
            assert (tmp_path / f"history-{mid}-compact.parquet").exists()
