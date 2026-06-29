"""Tests for the signal-level (alpha) backtest."""
import httpx
import pytest

from conftest import SAMPLE_MARKET
from marketlens.backtest import AlphaConfig, AlphaStrategy
from marketlens.backtest._bar import _METRICS_MAX_RANGE_MS, iter_bars

MIN = 60_000


# ── Helpers ──────────────────────────────────────────────────────

def _resolved_market(win_index=0, open_t=0, close_t=11 * MIN):
    return {
        **SAMPLE_MARKET,
        "id": "m1",
        "status": "resolved",
        "series_id": None,
        "subtype": "up_or_down",
        "winning_outcome_index": win_index,
        "winning_outcome": "Yes" if win_index == 0 else "No",
        "open_time": open_t,
        "close_time": close_t,
        "resolved_at": close_t,
    }


def _metric(t, mid):
    return {
        "t": t, "best_bid": round(mid - 0.01, 4), "best_ask": round(mid + 0.01, 4),
        "spread": 0.02, "midpoint": mid, "bid_depth": 1e6, "ask_depth": 1e6,
        "bid_levels": 3, "ask_levels": 3,
    }


def _metrics_response(*metrics):
    return {"data": list(metrics), "meta": {"cursor": None, "has_more": False}}


_MIDS = [0.40, 0.42, 0.45, 0.43, 0.47, 0.50, 0.53, 0.55, 0.57, 0.58]


def _mock_market(mock_api, market, mids=_MIDS):
    mock_api.get("/markets/m1").mock(return_value=httpx.Response(200, json=market))
    bars = [_metric((i + 1) * MIN, m) for i, m in enumerate(mids)]
    mock_api.get("/markets/m1/orderbook/metrics").mock(
        return_value=httpx.Response(200, json=_metrics_response(*bars)))


def _metrics_parquet_bytes(mids=_MIDS):
    """A metrics export parquet (the server schema) for the offline path."""
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    n = len(mids)
    table = pa.table({
        "t": [(i + 1) * MIN for i in range(n)],
        "best_bid": [m - 0.01 for m in mids], "best_ask": [m + 0.01 for m in mids],
        "spread": [0.02] * n, "midpoint": list(mids),
        "bid_depth": [1e6] * n, "ask_depth": [1e6] * n,
        "bid_levels": [3] * n, "ask_levels": [3] * n,
    })
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


class LongYes(AlphaStrategy):
    def on_bar(self, ctx, market, bar):
        ctx.target_weight(0.5)


# ── End-to-end ───────────────────────────────────────────────────

class TestAlphaBacktest:
    def test_buy_and_settle_yes_win(self, mock_api, client):
        _mock_market(mock_api, _resolved_market(win_index=0))
        res = client.backtest(
            LongYes(), "m1", after=0, before=11 * MIN, initial_cash=10_000,
            resolution="1m", price="mid", fill="next",
        )
        assert res.total_trades >= 1
        assert len(res._settlements) == 1
        assert res._settlements[0].side.value == "YES"
        assert res.total_pnl > 0           # long YES into a YES resolution
        assert res.targets.get("mode") == "alpha"

    def test_time_series_metrics_populated(self, mock_api, client):
        _mock_market(mock_api, _resolved_market(win_index=0))
        res = client.backtest(
            LongYes(), "m1", after=0, before=11 * MIN, initial_cash=10_000,
            resolution="1m",
        )
        # One equity point per bar, and time-series (not per-settlement) ratios.
        assert len(res._equity_curve) == len(_MIDS)
        assert res.sharpe_ratio is not None
        assert res.turnover is not None and res.turnover > 0
        assert res.volatility is not None
        assert "turnover" in res.summary()

    def test_next_bar_fill_no_lookahead(self, mock_api, client):
        _mock_market(mock_api, _resolved_market(win_index=0))
        res = client.backtest(
            LongYes(), "m1", after=0, before=11 * MIN, initial_cash=10_000, fill="next",
        )
        first_fill = min(f.timestamp for o in res._orders for f in o.fills)
        assert first_fill >= 2 * MIN       # target set on bar 1 (t=60000) fills on bar 2

    def test_fill_close_same_bar(self, mock_api, client):
        _mock_market(mock_api, _resolved_market(win_index=0))
        res = client.backtest(
            LongYes(), "m1", after=0, before=11 * MIN, initial_cash=10_000, fill="close",
        )
        first_fill = min(f.timestamp for o in res._orders for f in o.fills)
        assert first_fill == MIN           # first bar is t=60000

    def test_target_flip_to_no(self, mock_api, client):
        class Flip(AlphaStrategy):
            def on_bar(self, ctx, market, bar):
                ctx.target_weight(0.5 if ctx.time < 5 * MIN else -0.3)

        _mock_market(mock_api, _resolved_market(win_index=1))   # resolves NO
        res = client.backtest(
            Flip(), "m1", after=0, before=11 * MIN, initial_cash=10_000,
        )
        sides = {f.side.value for o in res._orders for f in o.fills}
        assert "BUY_YES" in sides and "BUY_NO" in sides
        assert res._settlements[0].side.value == "NO"

    def test_target_position_shares(self, mock_api, client):
        class Buy100(AlphaStrategy):
            def on_bar(self, ctx, market, bar):
                ctx.target_position(100)

        _mock_market(mock_api, _resolved_market(win_index=0))
        res = client.backtest(Buy100(), "m1", after=0, before=11 * MIN, initial_cash=10_000)
        pos = res._settlements[0]
        assert pos.side.value == "YES" and abs(pos.shares - 100) < 1e-6

    def test_queue_position_rejected_for_alpha(self, client):
        # The guard raises before any HTTP, so no routes are mocked.
        with pytest.raises(ValueError, match="queue_position"):
            client.backtest(
                LongYes(), "m1", after=0, before=11 * MIN, initial_cash=10_000,
                queue_position=True,
            )

    def test_offline_downloads_server_export_and_matches_streaming(self, mock_api, client, tmp_path):
        _mock_market(mock_api, _resolved_market(win_index=0))
        stream = client.backtest(
            LongYes(), "m1", after=0, before=11 * MIN, initial_cash=10_000)
        # Offline: the export endpoint 302-redirects to a presigned bucket URL,
        # keyed by market+resolution (no time window), like the history export.
        bucket_url = "https://bucket.example/metrics/m1-1m.parquet"
        mock_api.get("/markets/m1/orderbook/metrics/export").mock(
            return_value=httpx.Response(302, headers={"Location": bucket_url}))
        mock_api.get(bucket_url).mock(
            return_value=httpx.Response(200, content=_metrics_parquet_bytes()))
        off = client.backtest(
            LongYes(), "m1", after=0, before=11 * MIN, initial_cash=10_000,
            data_dir=str(tmp_path))
        assert (tmp_path / "metrics-m1-1m.parquet").exists()
        assert abs(stream.total_pnl - off.total_pnl) < 1e-6
        assert stream.total_trades == off.total_trades

    def test_offline_export_not_ready_skips_market(self, mock_api, client, tmp_path):
        # The variant is being built: the export endpoint reports not-ready, so
        # the market is skipped (no crash), the same as a missing history file.
        mock_api.get("/markets/m1").mock(
            return_value=httpx.Response(200, json=_resolved_market(win_index=0)))
        mock_api.get("/markets/m1/orderbook/metrics/export").mock(
            return_value=httpx.Response(
                409, json={"error": {"code": "EXPORT_NOT_READY", "message": "pending"}}))
        res = client.backtest(
            LongYes(), "m1", after=0, before=11 * MIN, initial_cash=10_000,
            data_dir=str(tmp_path))
        assert res.total_trades == 0 and len(res._settlements) == 0

    def test_reference_price_available(self, mock_api, client):
        market = _resolved_market(win_index=0)
        market["underlying"] = "BTC"
        _mock_market(mock_api, market)
        mock_api.get("/reference/candles").mock(return_value=httpx.Response(200, json={
            "data": [
                {"symbol": "BTC", "timestamp": i * MIN, "open": 100.0, "high": 100.0,
                 "low": 100.0, "close": 100.0 + i, "volume": 1.0}
                for i in range(12)
            ],
            "meta": {"cursor": None, "has_more": False},
        }))
        seen = []

        class UsesRef(AlphaStrategy):
            def on_bar(self, ctx, market, bar):
                seen.append(ctx.reference_price())

        client.backtest(UsesRef(), "m1", after=0, before=11 * MIN, initial_cash=10_000)
        assert any(p is not None for p in seen), "reference_price should be available to AlphaStrategy"

    def test_bar_params_rejected_for_tick_strategy(self, client):
        from marketlens.backtest import Strategy

        class Tick(Strategy):
            def on_book(self, ctx, market, book):
                pass

        with pytest.raises(ValueError, match="only to an AlphaStrategy"):
            client.backtest(Tick(), "m1", after=0, before=1, initial_cash=10_000,
                            resolution="5m")

    def test_multi_strategy(self, mock_api, client):
        _mock_market(mock_api, _resolved_market(win_index=0))

        class Buy100(AlphaStrategy):
            def on_bar(self, ctx, market, bar):
                ctx.target_position(100)

        res = client.backtest(
            [LongYes(), Buy100()], "m1", after=0, before=11 * MIN, initial_cash=10_000,
            labels=["lw", "b1"],
        )
        assert len(res) == 2 and res.labels == ["lw", "b1"]


# ── Config + chunking units ──────────────────────────────────────

class TestAlphaConfig:
    def test_valid(self):
        AlphaConfig(resolution="1m", price="mid", fill="next").validate()
        AlphaConfig(resolution="1s", price="close", fill="close").validate()

    @pytest.mark.parametrize("kw", [
        {"price": "bad"}, {"fill": "bad"},
        {"resolution": "1s", "price": "mid"},   # 1s invalid for metrics
        {"resolution": "2m", "price": "close"},  # not a real bucket
    ])
    def test_invalid(self, kw):
        with pytest.raises(ValueError):
            AlphaConfig(**kw).validate()


class TestChunking:
    def test_metrics_window_chunked_under_span_cap(self):
        span = int(2.5 * 86_400_000)        # 2.5 days at 1m, cap is 1 day -> 3 chunks
        calls = []

        class _OB:
            def metrics(self, mid, *, after, before, resolution, **kw):
                calls.append((after, before))
                return [type("M", (), {
                    "t": t, "midpoint": 0.5, "spread": 0.0,
                    "bid_depth": 0.0, "ask_depth": 0.0,
                })() for t in range(after, before, MIN)]

        ob = _OB()
        bars = list(iter_bars(ob, None, "m", 0, span, resolution="1m", price="mid"))

        assert len(calls) == 3
        assert calls[0][0] == 0 and calls[-1][1] == span
        for (a0, b0), (a1, b1) in zip(calls, calls[1:]):
            assert b0 == a1                  # abut, no gap/overlap
            assert b0 - a0 == _METRICS_MAX_RANGE_MS["1m"]
        ts = [b.t for b in bars]
        assert len(ts) == len(set(ts)) == span // MIN
