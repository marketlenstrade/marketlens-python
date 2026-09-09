"""Coverage-aware answers: DataNotAvailableError, coverage fields on Market
and OrderBook, series manifest coverage, and the backtest engine's
skip-and-report behaviour for markets that hold no replayable data."""
from __future__ import annotations

import httpx
import pytest

from conftest import SAMPLE_MARKET, SAMPLE_ORDERBOOK, SAMPLE_SERIES
from marketlens import MarketLens
from marketlens.backtest import Strategy
from marketlens.backtest import _engine as engine_mod
from marketlens.backtest._engine import _coverage_gap
from marketlens.backtest._results import BacktestResult
from marketlens.exceptions import DataNotAvailableError, NotFoundError
from marketlens.types.market import Market
from marketlens.types.orderbook import OrderBook

T_OPEN, T_CLOSE = 1_776_218_400_000, 1_776_218_700_000

_DNA_BODY = {
    "error": {
        "code": "DATA_NOT_AVAILABLE",
        "message": "No order book at or before 5 for this market; the earliest book is at 9.",
        "status": 404,
        "requested_at": 5,
        "coverage": {"data_start": 9, "data_end": 99, "collection_tier": "streamed"},
    }
}


# ── errors and types ─────────────────────────────────────────────


def test_data_not_available_is_typed_and_still_a_not_found(mock_api, client):
    mock_api.get("/markets/abc-123/orderbook").mock(return_value=httpx.Response(404, json=_DNA_BODY))
    with pytest.raises(NotFoundError) as exc_info:
        client.orderbook.get("abc-123", at=5, nearest="before")
    err = exc_info.value
    assert isinstance(err, DataNotAvailableError)
    assert err.code == "DATA_NOT_AVAILABLE"
    assert err.requested_at == 5
    assert (err.data_start, err.data_end) == (9, 99)
    assert err.collection_tier == "streamed"
    assert err.details["coverage"]["data_start"] == 9
    sent = mock_api.calls[0].request.url.params
    assert sent.get("nearest") == "before" and sent.get("at") == "5"


def test_data_not_available_tolerates_a_bare_body(mock_api, client):
    mock_api.get("/markets/abc-123/orderbook").mock(
        return_value=httpx.Response(404, json={"error": {"code": "DATA_NOT_AVAILABLE", "message": "none"}})
    )
    with pytest.raises(DataNotAvailableError) as exc_info:
        client.orderbook.get("abc-123", at=5)
    err = exc_info.value
    assert err.data_start is None and err.data_end is None and err.collection_tier is None
    assert err.details == {}


def test_orderbook_carries_requested_at_and_nearest(mock_api, client):
    body = {**SAMPLE_ORDERBOOK, "requested_at": 1_699_999_000_000, "nearest": "after"}
    mock_api.get("/markets/abc-123/orderbook").mock(return_value=httpx.Response(200, json=body))
    book = client.orderbook.get("abc-123", at=1_699_999_000_000)
    assert book.nearest == "after"
    assert book.requested_at == 1_699_999_000_000
    assert book.as_of > book.requested_at
    # Older servers: fields default to None, nothing breaks.
    assert OrderBook.model_validate(SAMPLE_ORDERBOOK).nearest is None


def test_market_coverage_fields_default_none():
    m = Market.model_validate(SAMPLE_MARKET)
    assert m.data_start is None and m.data_end is None
    m = Market.model_validate({**SAMPLE_MARKET, "data_start": 1, "data_end": None})
    assert (m.data_start, m.data_end) == (1, None)


# ── series manifest coverage ─────────────────────────────────────


def test_empty_series_manifest_returns_a_result_with_coverage(mock_api, client, tmp_path):
    mock_api.get("/series/quiet/export").mock(return_value=httpx.Response(200, json={
        "ready": [], "pending": [], "failed": [], "rate_limited": [],
        "events_charged": 0, "rows_charged": 0,
        "coverage": {"data_start": 41, "data_end": None, "collection_tier": "mixed"},
    }))
    mock_api.get("/series/quiet").mock(return_value=httpx.Response(404, json={
        "error": {"code": "SERIES_NOT_FOUND", "message": "nope"}}))
    result = client.exports.download_series("quiet", after=60, before=200, data_dir=str(tmp_path), progress=False)
    assert result.ready == [] and result.pending == [] and result.rate_limited == []
    assert (result.coverage.data_start, result.coverage.data_end, result.coverage.collection_tier) == (41, None, "mixed")


def test_series_manifest_without_coverage_parses(mock_api, client, tmp_path):
    mock_api.get("/series/old/export").mock(return_value=httpx.Response(200, json={
        "ready": [], "pending": [], "failed": [], "events_charged": 0,
    }))
    result = client.exports.download_series("old", data_dir=str(tmp_path), progress=False, dry_run=True)
    assert result.coverage is None


# ── coverage gap rule ────────────────────────────────────────────


def _mk(**over):
    base = {**SAMPLE_MARKET, "id": "m", "status": "resolved", "open_time": T_OPEN, "close_time": T_CLOSE}
    base.update(over)
    return Market.model_validate(base)


def test_coverage_gap_rule():
    # Older server: never skip.
    assert _coverage_gap(_mk(), T_OPEN, T_CLOSE) is None
    # A market whose life misses the window is skipped before any request,
    # coverage fields or not (streaming would otherwise send before < after).
    assert _coverage_gap(_mk(), T_CLOSE + 1_000, T_CLOSE + 2_000) == "outside window"
    assert _coverage_gap(_mk(), T_OPEN - 2_000, T_OPEN - 1_000) == "outside window"
    # A collapsed life (close_time <= open_time) says nothing: never skipped
    # up front, and the streams fall back to the user window.
    from marketlens.backtest._engine import _effective_window
    collapsed = _mk(close_time=T_OPEN)
    assert _coverage_gap(collapsed, T_OPEN + 60_000, T_OPEN + 120_000) is None
    assert _effective_window(collapsed, T_OPEN + 60_000, T_OPEN + 120_000) == (T_OPEN + 60_000, T_OPEN + 120_000)
    assert _effective_window(_mk(), T_OPEN - 5, T_CLOSE + 5) == (T_OPEN, T_CLOSE)
    # Null start is unknown-or-none, never proof of absence: the stream decides.
    assert _coverage_gap(_mk(data_start=None), T_OPEN, T_CLOSE) is None
    # Coverage entirely after the window.
    assert _coverage_gap(_mk(data_start=T_CLOSE + 1, data_end=T_CLOSE + 9), T_OPEN, T_CLOSE) == "no coverage in window"
    # Coverage entirely before the window.
    assert _coverage_gap(_mk(data_start=T_OPEN - 9, data_end=T_OPEN - 1), T_OPEN, T_CLOSE) == "no coverage in window"
    # Starts inside the window: replayed as is, not reported (a late first
    # snapshot is the norm on prod, not a data gap).
    assert _coverage_gap(_mk(data_start=T_OPEN + 60_000, data_end=T_CLOSE + 5), T_OPEN, T_CLOSE) is None
    # Full coverage.
    assert _coverage_gap(_mk(data_start=T_OPEN - 5, data_end=T_CLOSE + 5), T_OPEN, T_CLOSE) is None
    # Full coverage, still open (end None).
    assert _coverage_gap(_mk(status="active", data_start=T_OPEN - 5, data_end=None), T_OPEN, T_CLOSE) is None
    # No user window: the market's own life is the window.
    assert _coverage_gap(_mk(data_start=T_CLOSE + 1, data_end=T_CLOSE + 9), None, None) == "no coverage in window"
    assert _coverage_gap(_mk(data_start=T_OPEN + 1, data_end=T_CLOSE), None, None) is None


# ── engine: skip and report ──────────────────────────────────────


class _Noop(Strategy):
    def on_book(self, ctx, market, book):
        pass


@pytest.fixture
def status_lines(monkeypatch):
    """Status lines go to stderr only when progress is on (the suite turns it
    off), so capture them at the source."""
    lines: list[str] = []
    monkeypatch.setattr(engine_mod, "_prep_status", lines.append)
    return lines


def _series_mocks(mock_api, markets, histories):
    rolling = {**SAMPLE_SERIES, "id": "s-1", "platform_series_id": "btc-up-or-down-5m",
               "is_rolling": True, "title": "BTC Up or Down 5m"}
    mock_api.get("/markets/btc-up-or-down-5m").mock(return_value=httpx.Response(404, json={
        "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"}}))
    mock_api.get("/series/btc-up-or-down-5m").mock(return_value=httpx.Response(200, json=rolling))
    mock_api.get("/markets").mock(return_value=httpx.Response(200, json={
        "data": markets, "meta": {"cursor": None, "has_more": False}}))
    routes = {}
    for mid, events in histories.items():
        routes[mid] = mock_api.get(f"/markets/{mid}/orderbook/history").mock(
            return_value=httpx.Response(200, json={"data": events, "meta": {"cursor": None, "has_more": False}}))
    return routes


_SNAP = {
    "type": "snapshot", "t": T_OPEN + 10, "is_reseed": False,
    "bids": [{"price": "0.4500", "size": "100.0000"}],
    "asks": [{"price": "0.4700", "size": "100.0000"}],
}


def _m(mid, **over):
    return {**SAMPLE_MARKET, "id": mid, "status": "resolved", "series_id": "s-1",
            "series_title": "BTC Up or Down 5m",
            "winning_outcome": "Yes", "winning_outcome_index": 0,
            "open_time": T_OPEN, "close_time": T_CLOSE, "resolved_at": T_CLOSE, **over}


def test_uncovered_market_is_skipped_and_listed(mock_api, client, status_lines):
    covered = _m("m-ok", data_start=T_OPEN - 5, data_end=T_CLOSE + 5)
    # Coverage entirely after this market's window portion: positive evidence.
    gone = _m("m-gone", data_start=T_CLOSE + 60_000, data_end=T_CLOSE + 90_000)
    # Unstamped (null start): must NOT be skipped up front; its stream decides.
    unstamped = _m("m-unstamped", data_start=None, data_end=None)
    late = _m("m-late", open_time=T_CLOSE, close_time=T_CLOSE + 300_000, resolved_at=T_CLOSE + 300_000,
              data_start=T_CLOSE + 120_000, data_end=T_CLOSE + 300_000)
    # No history route for m-gone: fetching it would be an unmocked request,
    # which respx rejects, so the skip is proven by the run passing.
    routes = _series_mocks(mock_api, [covered, gone, unstamped, late], {
        "m-ok": [_SNAP],
        "m-unstamped": [],
        "m-late": [{**_SNAP, "t": T_CLOSE + 120_000}],
    })

    result = client.backtest(_Noop(), "btc-up-or-down-5m", after=T_OPEN, before=T_CLOSE + 300_000,
                             initial_cash=1000, progress=False)

    reasons = {sk.market_id: sk.reason for sk in result.skipped}
    assert reasons == {"m-gone": "no coverage in window", "m-unstamped": "no events in window"}
    assert result.markets_skipped == 2
    assert result.summary()["markets_skipped"] == 2
    # The unstamped market was fetched, the late-starting one replayed as is.
    assert routes["m-unstamped"].call_count == 1
    assert routes["m-late"].call_count == 1
    gone_row = next(sk for sk in result.skipped if sk.market_id == "m-gone")
    assert gone_row.open_time == T_OPEN and gone_row.data_start == T_CLOSE + 60_000
    assert "Skipping 1 of 4 markets for 'BTC Up or Down 5m': no order book coverage in window" in status_lines
    assert not any("partially" in l for l in status_lines)


def test_pending_export_mid_series_is_skipped_not_fatal(mock_api, client):
    """Streaming used to abort the whole run on the first 409; data_dir mode
    already skipped and reported such markets. Both now agree."""
    ok = _m("m-ok", data_start=T_OPEN - 5, data_end=T_CLOSE + 5)
    building = _m("m-building", open_time=T_CLOSE, close_time=T_CLOSE + 300_000,
                  resolved_at=T_CLOSE + 300_000, data_start=T_CLOSE + 5, data_end=None)
    routes = _series_mocks(mock_api, [ok, building], {"m-ok": [_SNAP]})
    mock_api.get("/markets/m-building/orderbook/history").mock(return_value=httpx.Response(409, json={
        "error": {"code": "EXPORT_NOT_READY", "message": "Export not ready (status=pending)"}}))
    result = client.backtest(_Noop(), "btc-up-or-down-5m", after=T_OPEN, before=T_CLOSE + 300_000,
                             initial_cash=1000, progress=False)
    assert [(sk.market_id, sk.reason) for sk in result.skipped] == [("m-building", "export pending")]
    assert routes["m-ok"].call_count == 1


def test_store_stall_mid_series_is_skipped_not_fatal(mock_api, client, monkeypatch):
    """A 503 from the history store on one market (after the transport's own
    retries) skips that market like a pending export, instead of aborting a
    run that may be many markets deep."""
    import marketlens._base as base
    monkeypatch.setattr(base.time, "sleep", lambda _s: None)
    ok = _m("m-ok", data_start=T_OPEN - 5, data_end=T_CLOSE + 5)
    stalled = _m("m-stalled", open_time=T_CLOSE, close_time=T_CLOSE + 300_000,
                 resolved_at=T_CLOSE + 300_000, data_start=T_CLOSE + 5, data_end=T_CLOSE + 300_000)
    routes = _series_mocks(mock_api, [ok, stalled], {"m-ok": [_SNAP]})
    stalled_route = mock_api.get("/markets/m-stalled/orderbook/history").mock(return_value=httpx.Response(
        503, json={"error": {"code": "STORE_UNAVAILABLE", "message": "The history store did not respond in time."}},
        headers={"Retry-After": "5"}))
    result = client.backtest(_Noop(), "btc-up-or-down-5m", after=T_OPEN, before=T_CLOSE + 300_000,
                             initial_cash=1000, progress=False)
    assert [(sk.market_id, sk.reason) for sk in result.skipped] == [("m-stalled", "history store unavailable")]
    assert routes["m-ok"].call_count == 1
    assert stalled_route.call_count == 1 + client._http.max_retries


def test_old_server_payload_skips_nothing(mock_api, client):
    m1 = _m("m-1")  # no coverage fields at all
    routes = _series_mocks(mock_api, [m1], {"m-1": [_SNAP]})
    result = client.backtest(_Noop(), "btc-up-or-down-5m", after=T_OPEN, before=T_CLOSE,
                             initial_cash=1000, progress=False)
    assert result.skipped == [] and result.markets_skipped == 0
    assert "markets_skipped" not in result.summary()
    assert routes["m-1"].call_count == 1


def test_stream_with_no_events_is_reported(mock_api, client):
    m1 = _m("m-1", data_start=T_OPEN - 5, data_end=T_CLOSE + 5)
    _series_mocks(mock_api, [m1], {"m-1": []})
    result = client.backtest(_Noop(), "btc-up-or-down-5m", after=T_OPEN, before=T_CLOSE,
                             initial_cash=1000, progress=False)
    assert [(sk.market_id, sk.reason) for sk in result.skipped] == [("m-1", "no events in window")]
    assert result.markets_skipped == 1


def test_empty_manifest_finishes_with_zero_markets_and_a_hint(mock_api, client, tmp_path, status_lines):
    _series_mocks(mock_api, [], {})
    mock_api.get("/markets/btc-up-or-down-5m/export").mock(return_value=httpx.Response(404, json={
        "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"}}))
    mock_api.get("/series/btc-up-or-down-5m/export").mock(return_value=httpx.Response(200, json={
        "ready": [], "pending": [], "failed": [], "rate_limited": [],
        "events_charged": 0, "rows_charged": 0,
        "coverage": {"data_start": T_OPEN - 7_200_000, "data_end": None, "collection_tier": "streamed"},
    }))
    result = client.backtest(_Noop(), "btc-up-or-down-5m", after=T_OPEN, before=T_CLOSE,
                             initial_cash=1000, data_dir=str(tmp_path / "d"), progress=False)
    assert result.total_trades == 0 and result.skipped == []
    hint = next(l for l in status_lines if l.startswith("No markets for 'btc-up-or-down-5m'"))
    assert "its data runs" in hint and "still open" in hint
    # The result carries the span too, so a caller can tell an empty window apart.
    assert result.coverage == {"btc-up-or-down-5m": {"kind": "series", "data_start": T_OPEN - 7_200_000, "data_end": None, "collection_tier": "streamed"}}
    assert "coverage" in result.summary()


def test_reused_data_dir_empty_window_carries_coverage(mock_api, client, tmp_path, status_lines):
    """A data_dir that already holds files skips the download, so the span
    comes from a dry-run manifest, as in streaming mode; one line, once."""
    _series_mocks(mock_api, [], {})
    manifest = mock_api.get("/series/btc-up-or-down-5m/export").mock(return_value=httpx.Response(200, json={
        "ready": [], "pending": [], "failed": [], "rate_limited": [], "events_charged": 0, "rows_charged": 0,
        "coverage": {"data_start": T_OPEN - 7_200_000, "data_end": None, "collection_tier": "streamed"},
    }))
    d = tmp_path / "d"
    d.mkdir()
    (d / "history-m-0.parquet").write_bytes(b"")
    result = client.backtest(_Noop(), "btc-up-or-down-5m", after=T_OPEN, before=T_CLOSE,
                             initial_cash=1000, data_dir=str(d), progress=False)
    assert manifest.call_count == 1
    assert manifest.calls[0].request.url.params.get("dry_run") == "true"
    assert result.coverage == {"btc-up-or-down-5m": {"kind": "series", "data_start": T_OPEN - 7_200_000, "data_end": None, "collection_tier": "streamed"}}
    assert sum(1 for l in status_lines if l.startswith("No markets for 'btc-up-or-down-5m'")) == 1


def test_streaming_empty_window_carries_coverage_from_a_dry_run_manifest(mock_api, client, status_lines):
    """Streaming has no download, so an empty series.walk asks the manifest
    (dry run, nothing billed) for the series' data span."""
    _series_mocks(mock_api, [], {})
    manifest = mock_api.get("/series/btc-up-or-down-5m/export").mock(return_value=httpx.Response(200, json={
        "ready": [], "pending": [], "failed": [], "rate_limited": [], "events_charged": 0, "rows_charged": 0,
        "coverage": {"data_start": T_OPEN - 86_400_000, "data_end": None, "collection_tier": "mixed"},
    }))
    result = client.backtest(_Noop(), "btc-up-or-down-5m", after=T_OPEN, before=T_CLOSE, initial_cash=1000, progress=False)
    assert manifest.call_count == 1
    assert manifest.calls[0].request.url.params.get("dry_run") == "true"
    assert result.coverage == {"btc-up-or-down-5m": {"kind": "series", "data_start": T_OPEN - 86_400_000, "data_end": None, "collection_tier": "mixed"}}
    assert any(l.startswith("No markets for 'btc-up-or-down-5m'") for l in status_lines)


def test_pending_export_lands_in_skipped(mock_api, client, tmp_path):
    m1 = _m("m-1", data_start=T_OPEN - 5, data_end=T_CLOSE + 5)
    _series_mocks(mock_api, [m1], {})
    mock_api.get("/markets/btc-up-or-down-5m/export").mock(return_value=httpx.Response(404, json={
        "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"}}))
    mock_api.get("/series/btc-up-or-down-5m/export").mock(return_value=httpx.Response(200, json={
        "ready": [], "pending": [{"market_id": "m-1", "status": "pending"}], "failed": [],
        "rate_limited": [], "events_charged": 0, "rows_charged": 0,
    }))
    result = client.backtest(_Noop(), "btc-up-or-down-5m", after=T_OPEN, before=T_CLOSE,
                             initial_cash=1000, data_dir=str(tmp_path / "d"), progress=False)
    assert [(sk.market_id, sk.reason) for sk in result.skipped] == [("m-1", "export pending")]


def test_skipped_survives_save_and_load(mock_api, client, tmp_path):
    gone = _m("m-gone", data_start=T_CLOSE + 60_000, data_end=T_CLOSE + 90_000)
    _series_mocks(mock_api, [gone], {})
    result = client.backtest(_Noop(), "btc-up-or-down-5m", after=T_OPEN, before=T_CLOSE,
                             initial_cash=1000, progress=False)
    path = result.save(tmp_path / "run")
    loaded = BacktestResult.load(path)
    assert [(sk.market_id, sk.reason) for sk in loaded.skipped] == [("m-gone", "no coverage in window")]
    assert loaded.markets_skipped == 1


# ── MCP ──────────────────────────────────────────────────────────


def test_mcp_error_dict_carries_coverage_and_hint(mock_api, client):
    pytest.importorskip("mcp")
    import marketlens.mcp.server as srvmod

    mock_api.get("/markets/abc-123/orderbook").mock(return_value=httpx.Response(404, json=_DNA_BODY))
    srvmod._client = client
    try:
        srv = srvmod.build_server()
        out = srv._tool_manager.get_tool("get_orderbook").fn(market_id="abc-123", at=5)
    finally:
        srvmod._client = None
    assert out["error"] == "DataNotAvailableError"
    assert out["coverage"]["data_start"] == 9
    assert "9" in out["hint"]


def test_mcp_book_view_notes_a_forward_book():
    from marketlens.mcp import _format as fmt

    book = OrderBook.model_validate({**SAMPLE_ORDERBOOK, "requested_at": 1, "nearest": "after"})
    view = fmt.book_view(book, depth=2)
    assert view["nearest"] == "after" and view["requested_at"] == 1
    assert "book at data_start" in view["note"]
    plain = fmt.book_view(OrderBook.model_validate(SAMPLE_ORDERBOOK), depth=2)
    assert "note" not in plain and plain["nearest"] is None


# ── empty-data reporting: once per series target, once per direct market ──


def _event_series_mocks(mock_api, markets_by_event: dict):
    """A structured series whose events resolve to the given markets."""
    structured = {**SAMPLE_SERIES, "id": "s-strk", "platform_series_id": "btc-multi-strikes-weekly",
                  "is_rolling": False, "title": "BTC Strikes", "structured_type": "survival"}
    mock_api.get("/markets/btc-multi-strikes-weekly").mock(return_value=httpx.Response(404, json={
        "error": {"code": "MARKET_NOT_FOUND", "message": "Not found"}}))
    mock_api.get("/series/btc-multi-strikes-weekly").mock(return_value=httpx.Response(200, json=structured))
    mock_api.get("/events").mock(return_value=httpx.Response(200, json={
        "data": [{"id": eid, "platform": "polymarket", "platform_event_id": eid, "title": eid, "category": "Crypto",
                  "series_id": "s-strk", "series_title": "BTC Strikes", "series_recurrence": "weekly",
                  "market_count": len(ms), "start_date": T_OPEN, "end_date": T_CLOSE,
                  "created_at": T_OPEN, "updated_at": T_OPEN} for eid, ms in markets_by_event.items()],
        "meta": {"cursor": None, "has_more": False}}))
    for eid, ms in markets_by_event.items():
        mock_api.get(f"/events/{eid}/markets").mock(return_value=httpx.Response(200, json={
            "data": ms, "meta": {"cursor": None, "has_more": False}}))


def test_single_market_outside_window_reports_once_by_name(mock_api, client, status_lines):
    m = _m("m-1", data_start=T_OPEN - 5, data_end=T_CLOSE + 5, collection_tier="streamed")
    mock_api.get("/markets/m-1").mock(return_value=httpx.Response(200, json=m))
    result = client.backtest(_Noop(), "m-1", after=T_CLOSE + 1_000, before=T_CLOSE + 2_000, initial_cash=1000, progress=False)
    lines = [l for l in status_lines if l.startswith("Market '")]
    assert len(lines) == 1 and "(m-1)" in lines[0] and "outside window" in lines[0] and "its data runs" in lines[0]
    assert not any(l.startswith("Skipping") for l in status_lines)
    assert result.coverage == {"m-1": {"kind": "market", "data_start": T_OPEN - 5, "data_end": T_CLOSE + 5, "collection_tier": "streamed"}}


def test_list_of_markets_reports_once_per_market_even_in_one_series(mock_api, client, status_lines):
    ids = ["m-a", "m-b", "m-c"]
    for mid in ids:
        mock_api.get(f"/markets/{mid}").mock(return_value=httpx.Response(200, json=_m(mid, data_start=T_OPEN - 5, data_end=T_CLOSE + 5)))
        mock_api.get(f"/markets/{mid}/orderbook/history").mock(return_value=httpx.Response(200, json={
            "data": [], "meta": {"cursor": None, "has_more": False}}))
    result = client.backtest(_Noop(), ids, after=T_OPEN, before=T_CLOSE, initial_cash=1000, progress=False)
    lines = [l for l in status_lines if l.startswith("Market '")]
    assert len(lines) == 3 and all("no events in window" in l for l in lines)
    assert sorted(sk.market_id for sk in result.skipped) == ids
    assert not any(l.startswith("'BTC Up or Down 5m'") for l in status_lines)


def test_series_run_groups_stream_time_skips_into_one_line(mock_api, client, status_lines):
    ok = _m("m-ok", data_start=T_OPEN - 5, data_end=T_CLOSE + 5)
    quiet = [_m(f"m-q{i}", data_start=T_OPEN - 5, data_end=T_CLOSE + 5) for i in range(3)]
    _series_mocks(mock_api, [ok, *quiet], {"m-ok": [_SNAP], **{q["id"]: [] for q in quiet}})
    result = client.backtest(_Noop(), "btc-up-or-down-5m", after=T_OPEN, before=T_CLOSE, initial_cash=1000, progress=False)
    grouped = [l for l in status_lines if l.startswith("'BTC Up or Down 5m'")]
    assert grouped == ["'BTC Up or Down 5m': 3 of 4 markets contributed no data in the window (3 no events in window)"]
    assert not any(l.startswith("Market '") for l in status_lines)
    assert result.markets_skipped == 3


def test_structured_series_with_no_markets_reports_once_with_its_span(mock_api, client, status_lines):
    _event_series_mocks(mock_api, {})
    mock_api.get("/series/btc-multi-strikes-weekly/export").mock(return_value=httpx.Response(200, json={
        "ready": [], "pending": [], "failed": [], "rate_limited": [], "events_charged": 0, "rows_charged": 0,
        "coverage": {"data_start": T_OPEN - 86_400_000, "data_end": T_CLOSE, "collection_tier": "streamed"},
    }))
    result = client.backtest(_Noop(), "btc-multi-strikes-weekly", after=T_OPEN, before=T_CLOSE, initial_cash=1000, progress=False)
    lines = [l for l in status_lines if l.startswith("No markets for 'btc-multi-strikes-weekly'")]
    assert len(lines) == 1 and "its data runs" in lines[0]
    assert result.coverage["btc-multi-strikes-weekly"]["data_start"] == T_OPEN - 86_400_000


def test_multi_strategy_run_announces_once(mock_api, client, status_lines):
    _series_mocks(mock_api, [], {})
    mock_api.get("/series/btc-up-or-down-5m/export").mock(return_value=httpx.Response(200, json={
        "ready": [], "pending": [], "failed": [], "rate_limited": [], "events_charged": 0, "rows_charged": 0,
        "coverage": {"data_start": T_OPEN - 86_400_000, "data_end": None, "collection_tier": "streamed"},
    }))
    client.backtest([_Noop(), _Noop()], "btc-up-or-down-5m", labels=["a", "b"], after=T_OPEN, before=T_CLOSE, initial_cash=1000, progress=False)
    assert sum(1 for l in status_lines if l.startswith("No markets for 'btc-up-or-down-5m'")) == 1


async def test_async_engine_reports_the_same_way(mock_api, status_lines):
    from marketlens import AsyncMarketLens
    from conftest import BASE_URL
    ok = _m("m-ok", data_start=T_OPEN - 5, data_end=T_CLOSE + 5)
    quiet = [_m(f"m-q{i}", data_start=T_OPEN - 5, data_end=T_CLOSE + 5) for i in range(2)]
    _series_mocks(mock_api, [ok, *quiet], {"m-ok": [_SNAP], **{q["id"]: [] for q in quiet}})
    mock_api.get("/markets/m-solo").mock(return_value=httpx.Response(200, json=_m("m-solo", series_id="s-9", series_title="Other", data_start=T_OPEN - 5, data_end=T_CLOSE + 5)))
    mock_api.get("/markets/m-solo/orderbook/history").mock(return_value=httpx.Response(200, json={
        "data": [], "meta": {"cursor": None, "has_more": False}}))
    c = AsyncMarketLens(api_key="mk_test_key", base_url=BASE_URL)
    try:
        result = await c.backtest(_Noop(), ["btc-up-or-down-5m", "m-solo"], after=T_OPEN, before=T_CLOSE, initial_cash=1000, progress=False)
    finally:
        await c.close()
    assert [l for l in status_lines if l.startswith("'BTC Up or Down 5m'")] == [
        "'BTC Up or Down 5m': 2 of 3 markets contributed no data in the window (2 no events in window)"]
    assert len([l for l in status_lines if l.startswith("Market '") and "(m-solo)" in l]) == 1
    assert result.markets_skipped == 3


def test_streams_stop_at_data_end_in_both_modes(mock_api, client, tmp_path):
    """A padded close_time no longer feeds the settled tail: the streaming
    request and the offline replay both end at data_end."""
    early_end = T_OPEN + 120_000
    m = _m("m-1", data_start=T_OPEN, data_end=early_end, close_time=T_CLOSE + 8 * 3_600_000)
    mock_api.get("/markets/m-1").mock(return_value=httpx.Response(200, json=m))
    route = mock_api.get("/markets/m-1/orderbook/history").mock(return_value=httpx.Response(200, json={
        "data": [_SNAP, {**_SNAP, "t": early_end + 5_000}], "meta": {"cursor": None, "has_more": False}}))
    seen: list[int] = []

    class Rec(Strategy):
        def on_book(self, ctx, market, book):
            seen.append(book.as_of)

    client.backtest(Rec(), "m-1", after=T_OPEN, before=T_CLOSE + 8 * 3_600_000, initial_cash=1000, progress=False)
    assert int(route.calls[0].request.url.params["before"]) == early_end
    assert seen and max(seen) < early_end


def test_every_target_carries_its_span(mock_api, client, status_lines):
    """A series that replayed carries the span of its markets; a direct
    market target carries its own; the empty-window entry is unchanged."""
    ms = [_m("m-a", data_start=T_OPEN, data_end=T_CLOSE + 5, collection_tier="streamed"),
          _m("m-b", data_start=T_OPEN + 60_000, data_end=T_CLOSE + 9, collection_tier="polled")]
    _series_mocks(mock_api, ms, {"m-a": [_SNAP], "m-b": [{**_SNAP, "t": T_OPEN + 70_000}]})
    mock_api.get("/markets/m-solo").mock(return_value=httpx.Response(200, json=_m(
        "m-solo", series_id="s-9", series_title="Other", data_start=T_OPEN - 5, data_end=None, collection_tier="streamed")))
    mock_api.get("/markets/m-solo/orderbook/history").mock(return_value=httpx.Response(200, json={
        "data": [_SNAP], "meta": {"cursor": None, "has_more": False}}))
    result = client.backtest(_Noop(), ["btc-up-or-down-5m", "m-solo"], after=T_OPEN, before=T_CLOSE, initial_cash=1000, progress=False)
    assert result.coverage == {
        "btc-up-or-down-5m": {"kind": "series", "data_start": T_OPEN, "data_end": T_CLOSE + 9, "collection_tier": "mixed"},
        "m-solo": {"kind": "market", "data_start": T_OPEN - 5, "data_end": None, "collection_tier": "streamed"},
    }
    assert "coverage" in result.summary()
