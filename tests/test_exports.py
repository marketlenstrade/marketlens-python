"""Tests for download progress and exports flow."""
from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from conftest import BASE_URL
from marketlens._constants import DEFAULT_TIMEOUT, DOWNLOAD_TIMEOUT


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

    def test_market_download_writes_parquet_compact_default(self, mock_api, client, tmp_path):
        # Default coalesce=True writes the -compact variant.
        mock_api.get("/markets/abc-123/export").mock(
            return_value=httpx.Response(200, content=b"PAR1fake")
        )
        mock_api.get("/markets/abc-123").mock(
            return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})
        )
        out = client.exports.download("abc-123", path=str(tmp_path), progress=False)
        assert (out / "history-abc-123-compact.parquet").exists()
        # And the export request carried coalesce=true.
        export_urls = [str(c.request.url) for c in mock_api.calls if "/export" in str(c.request.url)]
        assert export_urls and "coalesce=true" in export_urls[0]

    def test_market_download_full_when_coalesce_false(self, mock_api, client, tmp_path):
        mock_api.get("/markets/abc-123/export").mock(
            return_value=httpx.Response(200, content=b"PAR1fake")
        )
        mock_api.get("/markets/abc-123").mock(
            return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})
        )
        out = client.exports.download(
            "abc-123", path=str(tmp_path), progress=False, coalesce=False,
        )
        assert (out / "history-abc-123.parquet").exists()
        export_urls = [str(c.request.url) for c in mock_api.calls if "/export" in str(c.request.url)]
        assert export_urls and "coalesce" not in export_urls[0]

    def test_download_uses_download_timeout(self, mock_api, client, tmp_path):
        """Download paths must override the per-call read timeout to ``None``
        so streaming exports aren't cut off mid-flight."""
        mock_api.get("/markets/abc/export").mock(
            return_value=httpx.Response(200, content=b"x" * 32)
        )
        # No reporter → unstreamed path.
        client._http.download("/markets/abc/export", tmp_path / "f.bin")
        recorded = mock_api.calls[-1].request.extensions["timeout"]
        assert recorded["read"] is None  # DOWNLOAD_TIMEOUT.read
        assert recorded["connect"] == DOWNLOAD_TIMEOUT.connect

    def test_streamed_download_uses_download_timeout(self, mock_api, client, tmp_path):
        body = b"y" * 64
        mock_api.get("/markets/abc/export").mock(
            return_value=httpx.Response(200, content=body, headers={"Content-Length": str(len(body))})
        )
        client._http.download(
            "/markets/abc/export", tmp_path / "f.bin",
            reporter=_RecordingReporter(), label="x",
        )
        recorded = mock_api.calls[-1].request.extensions["timeout"]
        assert recorded["read"] is None

    def test_non_download_uses_default_timeout(self, mock_api, client):
        """Non-download requests keep the strict 30 s read timeout."""
        mock_api.get("/markets/abc-123").mock(
            return_value=httpx.Response(200, json={"id": "abc-123"})
        )
        client._http.get("/markets/abc-123")
        recorded = mock_api.calls[-1].request.extensions["timeout"]
        assert recorded["read"] == DEFAULT_TIMEOUT.read  # 30.0

    def test_series_download_extracts_zip(self, mock_api, client, tmp_path):
        # Default coalesce=True → server emits -compact.parquet entries.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("history-m1-compact.parquet", b"PAR1m1")
            zf.writestr("history-m2-compact.parquet", b"PAR1m2")
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
        assert (out / "history-m1-compact.parquet").read_bytes() == b"PAR1m1"
        assert (out / "history-m2-compact.parquet").read_bytes() == b"PAR1m2"
        # Streaming extract leaves no leftover .part files.
        assert not list(tmp_path.glob("*.part"))
        # No legacy intermediate zip on disk either.
        assert not list(tmp_path.glob("_series-*.zip.tmp"))

    def test_streaming_error_response_raises_clean_exception(self, mock_api, client, tmp_path):
        """A 4xx during a streaming download must surface as the proper SDK
        exception rather than crashing on ``ResponseNotRead``. Without
        explicit ``response.read()`` before parsing JSON, streaming-mode
        httpx responses don't auto-buffer their body."""
        from marketlens.exceptions import NotFoundError
        mock_api.get("/series/empty-window/export").mock(
            return_value=httpx.Response(
                404,
                json={"error": {"code": "NOT_FOUND", "message": "No markets in window"}},
            )
        )
        with pytest.raises(NotFoundError):
            client.exports.download_series(
                "empty-window",
                after="2026-04-12T00:00:00Z", before="2026-04-11T00:00:00Z",
                path=str(tmp_path), progress=False,
            )

    def test_series_download_skips_existing_files(self, mock_api, client, tmp_path):
        """Resume behavior: a member already on disk is left alone."""
        # Pre-populate one member with sentinel content (simulating partial
        # progress from an earlier interrupted download).
        (tmp_path / "history-m1-compact.parquet").write_bytes(b"OLD-ALREADY-ON-DISK")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("history-m1-compact.parquet", b"WOULD-OVERWRITE")
            zf.writestr("history-m2-compact.parquet", b"NEW-MEMBER")
        zip_bytes = buf.getvalue()

        mock_api.get("/series/btc-daily/export").mock(
            return_value=httpx.Response(200, content=zip_bytes)
        )
        mock_api.get("/series/btc-daily").mock(
            return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})
        )

        out = client.exports.download_series("btc-daily", path=str(tmp_path), progress=False)
        # Pre-existing file untouched
        assert (out / "history-m1-compact.parquet").read_bytes() == b"OLD-ALREADY-ON-DISK"
        # Missing member written
        assert (out / "history-m2-compact.parquet").read_bytes() == b"NEW-MEMBER"
        assert not list(tmp_path.glob("*.part"))
