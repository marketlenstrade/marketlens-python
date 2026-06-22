"""Unit tests for the (series, subtype) cohort resolver added for sports.

Exercises _resolve_cohort directly with a fake client (no HTTP), covering:
subtype filtering, the infer/raise path for multi-subtype series, the
client-side defensive filter, time-window selection, and lane/group keys.
"""
from __future__ import annotations

import pytest

from conftest import SAMPLE_MARKET
from marketlens.backtest import BacktestConfig, BacktestEngine, Strategy
from marketlens.types.market import Market


class _Noop(Strategy):
    pass


class _FakeList:
    def __init__(self, markets):
        self._markets = markets

    def to_list(self):
        return self._markets


class _FakeMarkets:
    """Returns ALL markets regardless of the subtype/series_id params, so the
    resolver's client-side filtering is what's under test (correct whether or
    not the server narrows by subtype)."""
    def __init__(self, markets):
        self._markets = markets
        self.calls = []

    def list(self, **params):
        self.calls.append(params)
        return _FakeList(self._markets)


class _FakeClient:
    def __init__(self, markets):
        self.markets = _FakeMarkets(markets)


class _FakeSeries:
    id = "series-mlb"
    title = "MLB"


def _mkt(mid, subtype, open_t, close_t):
    d = dict(SAMPLE_MARKET)
    d.update(
        id=mid, subtype=subtype, open_time=open_t, close_time=close_t,
        series_id="series-mlb",
    )
    return Market.model_validate(d)


def _engine():
    return BacktestEngine(_Noop(), BacktestConfig())


def _ids(lanes):
    return {m.id for lane in lanes for m in lane}


def test_explicit_subtype_filters_and_packs_lanes():
    markets = [
        _mkt("ml1", "moneyline", 100_000, 200_000),
        _mkt("sp1", "spread", 100_000, 200_000),
        _mkt("ml2", "moneyline", 150_000, 250_000),  # overlaps ml1 -> own lane
        _mkt("ml3", "moneyline", 300_000, 400_000),  # disjoint -> shares ml1's lane
        _mkt("tot1", "total:runs", 100_000, 200_000),
    ]
    eng = _engine()
    client = _FakeClient(markets)
    lanes = eng._resolve_cohort(client, "mlb", _FakeSeries(), "moneyline")

    # Only the moneyline cohort, never the spread/total markets.
    assert _ids(lanes) == {"ml1", "ml2", "ml3"}
    # Server filter is requested (optimisation) ...
    assert client.markets.calls[0].get("subtype") == "moneyline"
    # ... but selection is correct even though the fake returns everything.
    # Interval colouring: ml1+ml3 disjoint share a lane, ml2 overlaps -> 2 lanes.
    assert len(lanes) == 2
    # Group keys isolate the cohort for per-lane finalisation.
    for mid in _ids(lanes):
        assert eng._market_group[mid] == f"cohort:series-mlb:moneyline:{_lane_of(lanes, mid)}"


def _lane_of(lanes, mid):
    for i, lane in enumerate(lanes):
        if any(m.id == mid for m in lane):
            return i
    raise AssertionError(mid)


def test_infer_raises_when_series_mixes_subtypes():
    markets = [
        _mkt("ml1", "moneyline", 100_000, 200_000),
        _mkt("sp1", "spread", 100_000, 200_000),
        _mkt("junk", "rest", 100_000, 200_000),
    ]
    eng = _engine()
    with pytest.raises(ValueError, match="multiple subtypes"):
        eng._resolve_cohort(_FakeClient(markets), "mlb", _FakeSeries(), None)


def test_infer_uses_sole_subtype_ignoring_rest():
    # A single-nature series (e.g. weather=density) with a stray 'rest' market
    # resolves without a subtype argument.
    markets = [
        _mkt("a", "density", 100_000, 200_000),
        _mkt("b", "density", 300_000, 400_000),
        _mkt("junk", "rest", 100_000, 200_000),
    ]
    eng = _engine()
    lanes = eng._resolve_cohort(_FakeClient(markets), "wx", _FakeSeries(), None)
    assert _ids(lanes) == {"a", "b"}


def test_time_window_excludes_out_of_range_markets():
    markets = [
        _mkt("old", "moneyline", 1_000_000, 1_100_000),
        _mkt("inwin", "moneyline", 2_000_000, 2_100_000),
        _mkt("future", "moneyline", 9_000_000, 9_100_000),
    ]
    eng = _engine()
    lanes = eng._resolve_cohort(
        _FakeClient(markets), "mlb", _FakeSeries(), "moneyline",
        after=1_500_000, before=5_000_000,
    )
    assert _ids(lanes) == {"inwin"}
