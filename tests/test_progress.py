"""Tests for the progress reporter module."""
from __future__ import annotations

from marketlens._progress import (
    _NullReporter,
    _RichReporter,
    make_reporter,
)


class TestMakeReporter:
    def test_disabled_returns_null(self):
        r = make_reporter(enabled=False)
        assert isinstance(r, _NullReporter)

    def test_env_disable_returns_null(self, monkeypatch):
        monkeypatch.setenv("MARKETLENS_PROGRESS", "0")
        r = make_reporter(enabled=True)
        assert isinstance(r, _NullReporter)

    def test_env_disable_variants(self, monkeypatch):
        for val in ("0", "false", "no", "off", "FALSE", "Off"):
            monkeypatch.setenv("MARKETLENS_PROGRESS", val)
            assert isinstance(make_reporter(enabled=True), _NullReporter)

    def test_no_tty_returns_null(self, monkeypatch):
        # We're already running in pytest with no TTY; just confirm the
        # default behaviour returns null when MARKETLENS_PROGRESS is unset.
        monkeypatch.delenv("MARKETLENS_PROGRESS", raising=False)
        # In a CI/test context isatty() is False and we're not in Jupyter.
        r = make_reporter(enabled=True)
        assert isinstance(r, _NullReporter)


class TestNullReporter:
    """Confirm the no-op reporter swallows all calls without raising."""

    def test_full_lifecycle_no_raise(self):
        r = _NullReporter()
        with r:
            r.fetched("m1", 10)
            r.consumed("m1", 5)
            r.market_started("m1", "label")
            r.market_fetch_done("m1")
            r.market_finished("m1")
            r.download_started("file", 1024)
            r.download_progress(512)
            r.download_finished()


class TestRichReporter:
    """Smoke tests against the real rich-based reporter (rich is a dev dep)."""

    def test_bars_advance_in_markets_unit(self):
        """Both bars are denominated in markets — Fetching advances on
        ``market_fetch_done``, Backtesting on ``market_finished``.
        Consistent across every backtest shape regardless of total
        event counts."""
        r = _RichReporter(n_markets=3)
        with r:
            r.market_started("m1", "m1")
            # Events flowing for rate display only — bars don't move.
            r.fetched("m1", 1000)
            r.consumed("m1", 500)
            assert r._fetched_markets == 0
            assert r._consumed_markets == 0

            # Prefetcher exhausts m1 → Fetching advances 0→1.
            r.market_fetch_done("m1")
            assert r._fetched_markets == 1

            # Stream finishes for m1 → Backtesting advances 0→1.
            r.market_finished("m1")
            assert r._consumed_markets == 1

            # Same shape for m2.
            r.market_started("m2", "m2")
            r.market_fetch_done("m2")
            r.market_finished("m2")
            assert r._fetched_markets == 2
            assert r._consumed_markets == 2

    def test_event_counters_tally(self):
        """Raw event counts are tracked even though bars are in
        markets unit (kept for future use, e.g. summary stats)."""
        r = _RichReporter(n_markets=1)
        with r:
            r.market_started("m1", "m1")
            r.fetched("m1", 1000)
            r.fetched("m1", 500)
            r.consumed("m1", 800)
            r.consumed("m1", 100)
        assert r._fetched_events == 1500
        assert r._consumed_events == 900

    def test_replay_mode_skips_fetching_task(self):
        """In replay mode the data is on disk so the Fetching bar would
        sit at 0/N forever; it must not be created at all."""
        r = _RichReporter(n_markets=2)
        r.set_mode("replay")
        with r:
            r.market_started("m1", "m1")
            assert r._fetch_task is None, "Fetching bar must not exist in replay mode"
            assert r._consume_task is not None, "Backtesting bar must exist"
            r.market_finished("m1")
            assert r._consumed_markets == 1

    def test_batch_download_suppresses_per_file_bars(self):
        """When an aggregate batch bar is active, per-file byte bars must
        not be created (they'd thrash the screen)."""
        r = _RichReporter(n_markets=3)
        with r:
            r.batch_download_started("Downloading exports", 3)
            assert r._batch_task is not None
            # Per-file calls become no-ops.
            r.download_started("market m1", 1024)
            assert r._download_task is None
            r.download_progress(512)  # also a no-op, must not raise
            r.batch_download_advance()
            r.batch_download_advance()
            r.batch_download_advance()

    def test_lazy_progress_entry(self):
        """Progress container must not enter until the first task is added,
        so we don't show an empty bar during the network round-trip."""
        r = _RichReporter(n_markets=1)
        with r:
            assert r._started is False
            r.batch_download_started("Downloading exports", 1)
            assert r._started is True


class TestEngineIntegration:
    """End-to-end: confirm the prefetch+reporter wiring doesn't deadlock or
    drop events when progress is forced on against a mocked backend."""

    def test_backtest_with_progress_enabled_runs(self, mock_api, client, monkeypatch):
        import sys
        # Force-enable rich rendering by pretending we're in Jupyter, so the
        # reporter is a real _RichReporter instead of the no-op.
        monkeypatch.delenv("MARKETLENS_PROGRESS", raising=False)
        monkeypatch.setitem(
            sys.modules, "ipykernel",
            sys.modules.get("ipykernel") or type(sys)("ipykernel"),
        )

        from conftest import SAMPLE_MARKET
        from marketlens.backtest import Strategy

        market = {**SAMPLE_MARKET, "id": "mkt-1", "underlying": None}
        snapshot = {
            "type": "snapshot", "t": 1000, "is_reseed": False,
            "bids": [{"price": "0.6500", "size": "100.0000"}],
            "asks": [{"price": "0.6700", "size": "100.0000"}],
        }
        deltas = [
            {"type": "delta", "t": 1000 + i, "price": "0.6500",
             "size": str(100 + i) + ".0000", "side": "BUY"}
            for i in range(20)
        ]
        import httpx as _httpx
        mock_api.get("/markets/mkt-1").mock(
            return_value=_httpx.Response(200, json=market)
        )
        mock_api.get("/markets/mkt-1/orderbook/history").mock(
            return_value=_httpx.Response(
                200,
                json={"data": [snapshot] + deltas, "meta": {"cursor": None, "has_more": False}},
            )
        )

        events_seen = []

        class S(Strategy):
            def on_book(self, ctx, market, book):
                events_seen.append(book.as_of)

        result = client.backtest(
            S(), "mkt-1", initial_cash="1000",
            include_trades=False, fees=None, progress=True,
        )
        # All snapshot+delta events processed
        assert len(events_seen) == 1 + len(deltas)
        assert result is not None
