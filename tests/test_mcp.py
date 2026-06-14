"""Tests for the marketlens MCP server.

Covers tool registration, input-schema guardrails, model shaping, SDK-error
handling, and an end-to-end run_backtest subprocess round-trip (no network).
"""

import asyncio
import json

import httpx
import pytest

pytest.importorskip("mcp")

import marketlens.mcp.server as srvmod
from marketlens.mcp import _format as fmt
from tests.conftest import (
    SAMPLE_MARKET,
    SAMPLE_ORDERBOOK,
)

EXPECTED_TOOLS = {
    "search_markets", "get_market", "search_events", "search_series",
    "get_orderbook", "get_orderbook_metrics", "get_trades", "get_candles",
    "get_reference_candles", "get_signals", "get_surface",
    "strategy_reference", "run_backtest", "compare_backtests", "open_backtest",
}

BILLED_TOOLS = ["get_trades", "get_candles", "get_orderbook_metrics", "get_reference_candles"]


@pytest.fixture
def server(client):
    """A server wired to the respx-mocked test client."""
    srvmod._client = client
    srv = srvmod.build_server()
    yield srv
    srvmod._client = None


def tool_fn(srv, name):
    return srv._tool_manager.get_tool(name).fn


def tool_schema(srv, name):
    return srv._tool_manager.get_tool(name).parameters


# ── Registration & schema guardrails ──────────────────────────────


def test_all_tools_registered(server):
    names = {t.name for t in server._tool_manager.list_tools()}
    assert names == EXPECTED_TOOLS


@pytest.mark.parametrize("name", BILLED_TOOLS)
def test_billed_tools_require_time_bounds(server, name):
    required = tool_schema(server, name).get("required", [])
    assert "after" in required and "before" in required


def _enum(schema, param):
    """Pull the enum list for a param whose type may be wrapped in anyOf (| None)."""
    p = schema["properties"][param]
    if "enum" in p:
        return p["enum"]
    for branch in p.get("anyOf", []):
        if "enum" in branch:
            return branch["enum"]
    return None


def test_enum_params_advertised_in_schema(server):
    """A zero-context model should see valid values in the schema, not learn
    them from a rejected call."""
    assert set(_enum(tool_schema(server, "search_markets"), "status")) == {
        "active", "closed", "resolved"}
    assert set(_enum(tool_schema(server, "get_trades"), "side")) == {"BUY", "SELL"}
    assert set(_enum(tool_schema(server, "get_trades"), "order")) == {"asc", "desc"}
    assert set(_enum(tool_schema(server, "get_signals"), "surface_type")) == {
        "survival", "density", "barrier"}
    assert "1s" in _enum(tool_schema(server, "get_candles"), "resolution")
    # metrics resolution excludes sub-minute buckets
    assert "1s" not in _enum(tool_schema(server, "get_orderbook_metrics"), "resolution")


def test_run_backtest_requires_code_and_target(server):
    required = tool_schema(server, "run_backtest").get("required", [])
    assert set(required) == {"strategy_code", "target_id"}


# ── Data tools against the mocked API ──────────────────────────────


def test_search_markets_returns_brief(server, mock_api):
    mock_api.get("/markets").mock(
        return_value=httpx.Response(200, json={"data": [SAMPLE_MARKET], "meta": {"has_more": False}})
    )
    rows = tool_fn(server, "search_markets")(status="active", limit=5)
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["id"] == "abc-123"
    assert rows[0]["outcomes"] == [
        {"name": "Yes", "last_price": 0.65},
        {"name": "No", "last_price": 0.35},
    ]
    # Compact view drops heavy/rarely-needed fields.
    assert "tick_size" not in rows[0]


def test_search_markets_caps_limit(server, mock_api):
    route = mock_api.get("/markets").mock(
        return_value=httpx.Response(200, json={"data": [], "meta": {"has_more": False}})
    )
    tool_fn(server, "search_markets")(limit=10_000)
    # take is client-side, so assert the request did not ask for a huge page.
    assert "take" not in route.calls.last.request.url.params


def test_get_orderbook_includes_analytics(server, mock_api):
    mock_api.get("/markets/abc-123/orderbook").mock(
        return_value=httpx.Response(200, json=SAMPLE_ORDERBOOK)
    )
    book = tool_fn(server, "get_orderbook")(market_id="abc-123", depth=2)
    assert len(book["bids"]) == 2
    assert book["empty"] is False
    assert book["two_sided"] is True
    assert book["spread_bps"] is not None
    assert "microprice" in book and "imbalance" in book


def test_get_orderbook_empty_flagged(server, mock_api):
    empty = {**SAMPLE_ORDERBOOK, "bids": [], "asks": [], "bid_levels": 0, "ask_levels": 0}
    mock_api.get("/markets/abc-123/orderbook").mock(
        return_value=httpx.Response(200, json=empty)
    )
    book = tool_fn(server, "get_orderbook")(market_id="abc-123")
    assert book["empty"] is True
    assert book["two_sided"] is False
    # no real quote on either side; prices null, analytics omitted
    assert book["best_bid"] is None and book["best_ask"] is None
    assert book["midpoint"] is None
    assert "microprice" not in book


def test_get_orderbook_one_sided_nulls_missing_side(server, mock_api):
    """A one-sided book (asks only) is not 'empty', but the missing side's
    price is null rather than a placeholder that looks like a crossed quote."""
    one_sided = {**SAMPLE_ORDERBOOK, "bids": [], "bid_levels": 0, "best_bid": 0.5}
    mock_api.get("/markets/abc-123/orderbook").mock(
        return_value=httpx.Response(200, json=one_sided)
    )
    book = tool_fn(server, "get_orderbook")(market_id="abc-123")
    assert book["empty"] is False
    assert book["two_sided"] is False
    assert book["best_bid"] is None
    assert book["best_ask"] == SAMPLE_ORDERBOOK["best_ask"]
    assert book["midpoint"] is None
    assert "imbalance" not in book


def test_list_tool_error_passes_through_cleanly(server, mock_api):
    """A list-returning tool that hits an SDK error must surface the clean
    error dict over the wire, not a mangled output-schema validation failure.
    Regression: list[dict]-typed tools rejected the error dict as 'not a list'.
    """
    mock_api.get("/markets").mock(
        return_value=httpx.Response(
            400, json={"error": {"code": "INVALID_PARAMETER", "message": "bad range"}}
        )
    )
    res = asyncio.run(server.call_tool("search_markets", {"limit": 5}))
    content, structured = res if isinstance(res, tuple) else (res, None)
    payload = structured.get("result") if structured else json.loads(content[0].text)
    assert isinstance(payload, dict)
    assert "error" in payload and "message" in payload


def test_sdk_error_returned_as_dict(server, mock_api):
    mock_api.get("/markets/missing").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "MARKET_NOT_FOUND", "message": "nope"}}
        )
    )
    out = tool_fn(server, "get_market")(market_id="missing")
    assert out["error"] == "NotFoundError"
    assert "message" in out


# ── Formatters ─────────────────────────────────────────────────────


def test_surface_brief_omits_strikes():
    from marketlens.types.signal import Surface

    s = Surface.model_validate({
        "series_id": "s1", "event_id": "e1", "surface_type": "survival",
        "underlying": "BTC", "computed_at": 1, "expiry_ms": 2, "n_strikes": 3,
        "implied_mean": 100000.0, "strikes": [{"strike": 1, "raw_prob": 0.5,
        "fitted_prob": 0.5, "market_id": "m"}],
    })
    brief = fmt.surface_brief(s)
    assert "strikes" not in brief
    assert brief["n_strikes"] == 3
    assert "strikes" in fmt.surface_full(s)


# ── run_backtest ───────────────────────────────────────────────────


def test_run_backtest_disabled(server, monkeypatch):
    monkeypatch.setenv("MARKETLENS_MCP_DISABLE_BACKTEST", "1")
    out = tool_fn(server, "run_backtest")(strategy_code="x = 1", target_id="m")
    assert out["error"] == "BacktestDisabled"


def test_run_backtest_no_strategy_class(server, tmp_path):
    """End-to-end subprocess round-trip: a module with no Strategy subclass
    fails cleanly before any network call."""
    out = tool_fn(server, "run_backtest")(
        strategy_code="value = 42\n",
        target_id="some-market",
        artifacts_dir=str(tmp_path),
        timeout_s=120,
    )
    assert out["ok"] is False
    assert "No Strategy subclass" in out["message"]
    assert out["strategy_file"].endswith(".py")
    # every run that reaches the subprocess reports wall-clock runtime
    assert isinstance(out["elapsed_s"], (int, float))


def test_run_backtest_rejects_delimited_target(server):
    out = tool_fn(server, "run_backtest")(
        strategy_code="x=1", target_id="btc-up-or-down-5m,eth-up-or-down-5m",
    )
    assert out["error"] == "InvalidParameter"
    assert "list" in out["hint"]


def test_compare_backtests_requires_two_variants(server):
    out = tool_fn(server, "compare_backtests")(
        variants=[{"label": "a", "strategy_code": "x=1"}], target_id="m",
    )
    assert out["error"] == "InvalidParameter"


def test_compare_backtests_variant_needs_code(server, tmp_path):
    out = tool_fn(server, "compare_backtests")(
        variants=[{"label": "a", "strategy_code": "x=1"}, {"label": "b"}],
        target_id="m", artifacts_dir=str(tmp_path),
    )
    assert out["error"] == "InvalidParameter"
    assert "strategy_code" in out["message"]


def test_open_backtest_not_found(server, tmp_path):
    out = tool_fn(server, "open_backtest")(artifact_dir=str(tmp_path / "nope"))
    assert out["error"] == "NotFound"


def test_metrics_include_order_and_reject_counts():
    from types import SimpleNamespace
    from marketlens.mcp import _runner

    m = _runner._metrics(SimpleNamespace(_orders=[0, 0, 0], cash_rejected=7))
    assert m["total_orders"] == 3 and m["cash_rejected"] == 7


def test_warnings_flag_degenerate_runs():
    from types import SimpleNamespace
    from marketlens.mcp import _runner

    zero = SimpleNamespace(total_trades=0, markets_traded=0, _orders=[],
                           cash_rejected=0, total_fees=0.0, initial_cash=1000.0)
    assert any("0 trades" in w for w in _runner._warnings(zero))

    blowup = SimpleNamespace(total_trades=5, markets_traded=2, _orders=[0] * 5000,
                             cash_rejected=3000, total_fees=600.0, initial_cash=1000.0)
    msgs = _runner._warnings(blowup)
    assert any("order rate" in m for m in msgs)
    assert any("rejected" in m for m in msgs)
    assert any("Fees" in m for m in msgs)

    clean = SimpleNamespace(total_trades=10, markets_traded=10, _orders=[0] * 12,
                            cash_rejected=0, total_fees=2.0, initial_cash=1000.0)
    assert _runner._warnings(clean) == []


def test_by_series_annotates_with_slug_and_title():
    from types import SimpleNamespace
    from marketlens.mcp import _runner

    result = SimpleNamespace(
        by_series=lambda: {"uuid-1": {"total_pnl": 5.0, "profit_factor": float("inf")}}
    )
    client = SimpleNamespace(series=SimpleNamespace(
        get=lambda sid: SimpleNamespace(platform_series_id="btc-up-or-down-5m",
                                        title="BTC Up or Down 5m")
    ))
    row = _runner._by_series(result, client)["uuid-1"]
    assert row["series_slug"] == "btc-up-or-down-5m"
    assert row["series_title"] == "BTC Up or Down 5m"
    assert row["profit_factor"] == "inf"  # json-safe inf encoding preserved


def test_strategy_reference_describes_hooks(server):
    text = tool_fn(server, "strategy_reference")()
    assert "on_book" in text and "ctx.buy_yes" in text
    # gaps that caused guaranteed first-run failures in blind testing
    assert "shares" in text                       # Position field (not `size`)
    assert "buy_no at 1 - price" in text          # no-shorting / CTF quote idiom
    assert "latency_ms" in text                   # fills lag (not immediate)


def test_runner_error_payload_hints_on_export_not_ready():
    from marketlens.exceptions import ExportNotReadyError
    from marketlens.mcp import _runner

    # unexpected errors (e.g. a bug in the strategy code) keep the traceback,
    # carry no hint
    plain = _runner._error_payload(ValueError("boom"))
    assert plain["ok"] is False and "hint" not in plain
    assert "traceback" in plain

    # an expected API condition gets an actionable hint and drops the noisy
    # SDK-internal traceback
    exc = ExportNotReadyError(409, "EXPORT_NOT_READY", "Export not ready (status=pending)")
    payload = _runner._error_payload(exc)
    assert payload["error"] == "ExportNotReadyError"
    # the hint must not prescribe a misleading direction ("older")
    assert "window" in payload["hint"].lower()
    assert "older" not in payload["hint"] or "non-contiguous" in payload["hint"]
    assert "traceback" not in payload
