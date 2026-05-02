"""Tests for download progress and exports flow."""
from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from conftest import BASE_URL


class _RecordingReporter:
    """Captures download_started / download_progress calls."""

    def __init__(self):
        self.started: list[tuple[str, int | None]] = []
        self.progress: list[int] = []
        self.finished = 0

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def fetched(self, *a, **k): pass
    def consumed(self, *a, **k): pass
    def market_started(self, *a, **k): pass
    def market_fetch_done(self, *a, **k): pass
    def market_finished(self, *a, **k): pass
    def download_started(self, label, total): self.started.append((label, total))
    def download_progress(self, n): self.progress.append(n)
    def download_finished(self): self.finished += 1
    def status(self, *a, **k): pass


class TestStreamingDownload:
    """Confirm reporter-driven streaming download reports byte progress."""

    def test_download_reports_progress(self, mock_api, client, tmp_path):
        body = b"x" * 4096
        mock_api.get("/markets/abc/export").mock(
            return_value=httpx.Response(
                200, content=body, headers={"Content-Length": str(len(body))},
            )
        )

        rep = _RecordingReporter()
        client._http.download(
            "/markets/abc/export",
            tmp_path / "history-abc.parquet",
            reporter=rep, label="market abc",
        )

        assert rep.started == [("market abc", len(body))]
        assert rep.progress and rep.progress[-1] == len(body)
        assert rep.progress == sorted(rep.progress)  # monotonic
        assert rep.finished == 1
        assert (tmp_path / "history-abc.parquet").read_bytes() == body

    def test_no_reporter_uses_unstreamed_path(self, mock_api, client, tmp_path):
        body = b"y" * 256
        mock_api.get("/markets/abc/export").mock(
            return_value=httpx.Response(200, content=body)
        )
        client._http.download("/markets/abc/export", tmp_path / "out.bin")
        assert (tmp_path / "out.bin").read_bytes() == body


class TestExportsResource:
    """Smoke test that exports.download() runs end-to-end with progress=False."""

    def test_market_download_writes_parquet(self, mock_api, client, tmp_path):
        mock_api.get("/markets/abc-123/export").mock(
            return_value=httpx.Response(200, content=b"PAR1fake")
        )
        mock_api.get("/markets/abc-123").mock(
            return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})
        )
        out = client.exports.download("abc-123", path=str(tmp_path), progress=False)
        assert (out / "history-abc-123.parquet").exists()

    def test_series_download_extracts_zip(self, mock_api, client, tmp_path):
        # Build an in-memory zip with a single fake parquet
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("history-m1.parquet", b"PAR1m1")
            zf.writestr("history-m2.parquet", b"PAR1m2")
        zip_bytes = buf.getvalue()

        mock_api.get("/series/btc-daily/export").mock(
            return_value=httpx.Response(
                200, content=zip_bytes,
                headers={"Content-Length": str(len(zip_bytes))},
            )
        )
        # series.walk → 404 so the underlying-lookup path is skipped
        mock_api.get("/series/btc-daily").mock(
            return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})
        )
        out = client.exports.download_series("btc-daily", path=str(tmp_path), progress=False)
        assert (out / "history-m1.parquet").read_bytes() == b"PAR1m1"
        assert (out / "history-m2.parquet").read_bytes() == b"PAR1m2"
        # Temp zip is cleaned up
        assert not list(tmp_path.glob("_series-*.zip.tmp"))
