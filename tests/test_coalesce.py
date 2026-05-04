"""Tests for the trade-aligned coalesce data path.

Covers:
- Strategy detection (which strategies route to compact mode)
- Engine streaming-API request shape (coalesce=true vs absent)
- data_dir resolution priority (compact-vs-full filename selection)
- Hard-error path: queue_position + compact-only data
- Soft-warn path: full-only data + on_trade-only strategy
- BookReplay equivalence: full vs hand-coalesced events produce identical
  books at every trade and snapshot
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from conftest import BASE_URL, SAMPLE_MARKET
from marketlens import MarketLens
from marketlens.backtest import BacktestConfig, BacktestEngine, Strategy
from marketlens.backtest._strategy import _is_trade_only
from marketlens.backtest._types import OrderSide, OrderStatus
from marketlens.helpers.replay import OrderBookReplay
from marketlens.types.history import DeltaEvent, SnapshotEvent, TradeEvent
from marketlens.types.orderbook import PriceLevel


# ── _is_trade_only / detection ────────────────────────────────────


class TestStrategyDetection:
    def test_base_strategy_is_trade_only(self):
        assert _is_trade_only(Strategy())

    def test_on_book_override_disables(self):
        class S(Strategy):
            def on_book(self, ctx, market, book):
                pass

        assert _is_trade_only(S()) is False

    def test_on_trade_only_override_kept(self):
        class S(Strategy):
            def on_trade(self, ctx, market, book, trade):
                pass

        assert _is_trade_only(S()) is True

    def test_instance_attribute_override_disables(self):
        s = Strategy()
        s.on_book = lambda ctx, market, book: None  # type: ignore[method-assign]
        assert _is_trade_only(s) is False

    def test_inherited_override_propagates(self):
        class Base(Strategy):
            def on_book(self, ctx, market, book):
                pass

        class Leaf(Base):
            pass

        assert _is_trade_only(Leaf()) is False


# ── Engine compact_mode gating ────────────────────────────────────


class TestEngineCompactMode:
    def test_default_strategy_enables_compact(self):
        e = BacktestEngine(Strategy())
        assert e._compact_mode is True

    def test_on_book_strategy_disables_compact(self):
        class S(Strategy):
            def on_book(self, ctx, market, book):
                pass

        assert BacktestEngine(S())._compact_mode is False

    def test_queue_position_disables_compact(self):
        cfg = BacktestConfig(queue_position=True)
        assert BacktestEngine(Strategy(), cfg)._compact_mode is False

    def test_no_trades_disables_compact(self):
        cfg = BacktestConfig(include_trades=False)
        assert BacktestEngine(Strategy(), cfg)._compact_mode is False

    def test_explicit_coalesce_false_overrides_auto_detect(self):
        # Trade-only strategy would auto-detect compact, but coalesce=False forces full.
        cfg = BacktestConfig(coalesce=False)
        assert BacktestEngine(Strategy(), cfg)._compact_mode is False

    def test_explicit_coalesce_true_overrides_on_book(self):
        # on_book is overridden (would auto-detect full), but coalesce=True forces compact.
        class S(Strategy):
            def on_book(self, ctx, market, book):
                pass

        cfg = BacktestConfig(coalesce=True)
        assert BacktestEngine(S(), cfg)._compact_mode is True

    def test_explicit_coalesce_true_with_queue_position_raises(self):
        cfg = BacktestConfig(coalesce=True, queue_position=True)
        with pytest.raises(ValueError, match="queue_position=True"):
            BacktestEngine(Strategy(), cfg)

    def test_explicit_coalesce_true_without_trades_raises(self):
        cfg = BacktestConfig(coalesce=True, include_trades=False)
        with pytest.raises(ValueError, match="include_trades=False"):
            BacktestEngine(Strategy(), cfg)


class TestSubmissionBookPinning:
    """The fill price is anchored to the book the strategy saw at submission,
    regardless of mode or latency_ms. Latency only defers when the fill is
    recorded; it never sees a different book."""

    def _engine(self, strategy_cls, **cfg_kwargs):
        cfg = BacktestConfig(latency_ms=50, **cfg_kwargs)
        e = BacktestEngine(strategy_cls(), cfg)
        from marketlens.types.market import Market
        from marketlens.types.orderbook import OrderBook, PriceLevel
        e._current_market = Market.model_validate({**SAMPLE_MARKET, "id": "m1"})
        e._current_book = OrderBook(
            market_id="m1", platform="polymarket", as_of=1000,
            bids=[PriceLevel(price="0.4900", size="100.0000")],
            asks=[PriceLevel(price="0.5100", size="100.0000")],
            best_bid="0.4900", best_ask="0.5100",
            spread="0.0200", midpoint="0.5000",
            bid_depth="100.0000", ask_depth="100.0000",
            bid_levels=1, ask_levels=1,
        )
        e._current_time = 1000
        e._books["m1"] = e._current_book
        return e

    def test_submit_order_pins_book_per_order(self):
        e = self._engine(Strategy)
        order = e.submit_order(OrderSide.BUY_YES, "10.0000")
        # Order is pending (latency_ms=50) but the book is already pinned.
        assert order.status == OrderStatus.PENDING
        assert order.id in e._book_at_submission
        # And the pinned reference is the engine's current book at submission.
        assert e._book_at_submission[order.id] is e._current_book

    def test_pinned_book_survives_engine_book_mutation(self):
        e = self._engine(Strategy)
        original_book = e._current_book
        order = e.submit_order(OrderSide.BUY_YES, "10.0000")

        # Now simulate the engine moving forward — replace current_book.
        from marketlens.types.orderbook import OrderBook, PriceLevel
        e._current_book = OrderBook(
            market_id="m1", platform="polymarket", as_of=2000,
            bids=[PriceLevel(price="0.4000", size="100.0000")],
            asks=[PriceLevel(price="0.4100", size="100.0000")],
            best_bid="0.4000", best_ask="0.4100",
            spread="0.0100", midpoint="0.4050",
            bid_depth="100.0000", ask_depth="100.0000",
            bid_levels=1, ask_levels=1,
        )
        e._books["m1"] = e._current_book
        # The pinned book is unchanged — fill must still price against it.
        assert e._book_at_submission[order.id] is original_book
        assert e._fill_book(order) is original_book

    def test_book_pin_cleared_on_fill(self):
        e = self._engine(Strategy)
        # latency_ms=0 forces immediate fill via _fill_market_order.
        e._latency_ms = 0
        order = e.submit_order(OrderSide.BUY_YES, "10.0000")
        assert order.status == OrderStatus.FILLED
        assert order.id not in e._book_at_submission


# ── Engine streaming routes coalesce=true to history endpoint ─────


SNAPSHOT_EVENT = {
    "type": "snapshot", "t": 1000, "is_reseed": False,
    "bids": [{"price": "0.5000", "size": "100.0000"}],
    "asks": [{"price": "0.5100", "size": "100.0000"}],
}
TRADE_EVENT = {
    "type": "trade", "t": 1500, "id": "t1",
    "price": "0.5000", "size": "10.0000", "side": "SELL",
}


class TestEngineStreaming:
    def _setup(self, mock_api):
        market_id = SAMPLE_MARKET["id"]
        mock_api.get(f"/markets/{market_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_MARKET),
        )
        mock_api.get(f"/markets/{market_id}/orderbook/history").mock(
            return_value=httpx.Response(200, json={
                "data": [SNAPSHOT_EVENT, TRADE_EVENT],
                "meta": {"cursor": None, "has_more": False},
            }),
        )
        return market_id

    def test_trade_only_strategy_passes_coalesce_true(self, mock_api, client):
        market_id = self._setup(mock_api)
        BacktestEngine(Strategy()).run(client, market_id)
        history_calls = [
            c for c in mock_api.calls if "/orderbook/history" in str(c.request.url)
        ]
        assert history_calls
        assert "coalesce=true" in str(history_calls[0].request.url)

    def test_on_book_strategy_omits_coalesce(self, mock_api, client):
        class S(Strategy):
            def on_book(self, ctx, market, book):
                pass

        market_id = self._setup(mock_api)
        BacktestEngine(S()).run(client, market_id)
        history_calls = [
            c for c in mock_api.calls if "/orderbook/history" in str(c.request.url)
        ]
        assert history_calls
        assert "coalesce" not in str(history_calls[0].request.url)


# ── data_dir variant resolution ───────────────────────────────────


def _write_minimal_parquet(path: Path) -> None:
    """Write a single-snapshot parquet matching the engine schema."""
    schema = pa.schema([
        ("event_type", pa.string()),
        ("t", pa.int64()),
        ("price", pa.float64()),
        ("size", pa.float64()),
        ("side", pa.string()),
        ("trade_id", pa.string()),
        ("is_reseed", pa.bool_()),
        ("bids", pa.string()),
        ("asks", pa.string()),
    ])
    rows = {
        "event_type": ["snapshot"],
        "t": [1000],
        "price": [None],
        "size": [None],
        "side": [None],
        "trade_id": [None],
        "is_reseed": [False],
        "bids": [json.dumps([{"price": "0.5000", "size": "100.0000"}])],
        "asks": [json.dumps([{"price": "0.5100", "size": "100.0000"}])],
    }
    pq.write_table(pa.table(rows, schema=schema), path)


class TestDataDirResolution:
    def test_compact_strategy_prefers_compact_file(self, tmp_path):
        market_id = SAMPLE_MARKET["id"]
        _write_minimal_parquet(tmp_path / f"history-{market_id}.parquet")
        _write_minimal_parquet(tmp_path / f"history-{market_id}-compact.parquet")
        engine = BacktestEngine(Strategy())  # trade-only ⇒ compact_mode
        resolved = engine._resolve_history_file(tmp_path, market_id)
        assert resolved is not None
        assert resolved.name == f"history-{market_id}-compact.parquet"

    def test_on_book_strategy_prefers_full_file(self, tmp_path):
        class S(Strategy):
            def on_book(self, ctx, market, book):
                pass

        market_id = SAMPLE_MARKET["id"]
        _write_minimal_parquet(tmp_path / f"history-{market_id}.parquet")
        _write_minimal_parquet(tmp_path / f"history-{market_id}-compact.parquet")
        resolved = BacktestEngine(S())._resolve_history_file(tmp_path, market_id)
        assert resolved is not None
        assert resolved.name == f"history-{market_id}.parquet"

    def test_compact_strategy_falls_back_to_full(self, tmp_path, capsys):
        market_id = SAMPLE_MARKET["id"]
        _write_minimal_parquet(tmp_path / f"history-{market_id}.parquet")
        engine = BacktestEngine(Strategy())
        resolved = engine._resolve_history_file(tmp_path, market_id)
        assert resolved is not None
        assert resolved.name == f"history-{market_id}.parquet"
        # Targets recorded the chosen filename for non-TTY visibility.
        assert engine._targets["resolved_files"][market_id] == resolved.name
        # And we emitted a stderr note.
        captured = capsys.readouterr()
        assert "slower than necessary" in captured.err

    def test_queue_position_and_only_compact_hard_errors(self, tmp_path):
        market_id = SAMPLE_MARKET["id"]
        _write_minimal_parquet(tmp_path / f"history-{market_id}-compact.parquet")
        cfg = BacktestConfig(queue_position=True)

        class S(Strategy):
            def on_book(self, ctx, market, book):
                pass  # any on_book strategy disables compact mode

        engine = BacktestEngine(S(), cfg)
        with pytest.raises(ValueError, match="queue_position=True requires"):
            engine._resolve_history_file(tmp_path, market_id)

    def test_missing_files_returns_none(self, tmp_path):
        engine = BacktestEngine(Strategy())
        assert engine._resolve_history_file(tmp_path, "nonexistent") is None


# ── BookReplay equivalence over a hand-coalesced stream ───────────


def _to_history_event(d: dict):
    """Convert a SDK-shape event dict to a typed HistoryEvent."""
    if d["type"] == "snapshot":
        return SnapshotEvent(
            t=d["t"], is_reseed=d["is_reseed"],
            bids=[PriceLevel(price=l["price"], size=l["size"]) for l in d["bids"]],
            asks=[PriceLevel(price=l["price"], size=l["size"]) for l in d["asks"]],
        )
    if d["type"] == "delta":
        return DeltaEvent(t=d["t"], price=d["price"], size=d["size"], side=d["side"])
    return TradeEvent(
        t=d["t"], id=d["id"], price=d["price"], size=d["size"], side=d["side"],
    )


class TestReplayEquivalence:
    """Synthesise a small full firehose, hand-coalesce, and assert that
    the book reconstructed at every trade is byte-identical."""

    def _full_stream(self):
        # snapshot → 5 deltas churn @ 0.5000 BUY → trade → 3 deltas churn @
        # 0.5100 SELL → trade → snapshot reseed.
        return [
            {"type": "snapshot", "t": 1000, "is_reseed": False,
             "bids": [{"price": "0.5000", "size": "100.0000"}],
             "asks": [{"price": "0.5100", "size": "100.0000"}]},
            {"type": "delta", "t": 1100, "price": "0.5000", "size": "120.0000", "side": "BUY"},
            {"type": "delta", "t": 1200, "price": "0.5000", "size": "60.0000", "side": "BUY"},
            {"type": "delta", "t": 1300, "price": "0.5000", "size": "180.0000", "side": "BUY"},
            {"type": "delta", "t": 1400, "price": "0.5000", "size": "200.0000", "side": "BUY"},
            {"type": "delta", "t": 1450, "price": "0.5000", "size": "200.0000", "side": "BUY"},
            {"type": "trade", "t": 1500, "id": "t1", "price": "0.5000", "size": "10.0000", "side": "SELL"},
            {"type": "delta", "t": 1600, "price": "0.5100", "size": "80.0000", "side": "SELL"},
            {"type": "delta", "t": 1700, "price": "0.5100", "size": "200.0000", "side": "SELL"},
            {"type": "delta", "t": 1800, "price": "0.5100", "size": "120.0000", "side": "SELL"},
            {"type": "trade", "t": 2000, "id": "t2", "price": "0.5100", "size": "5.0000", "side": "BUY"},
            {"type": "snapshot", "t": 3000, "is_reseed": True,
             "bids": [{"price": "0.5050", "size": "300.0000"}],
             "asks": [{"price": "0.5150", "size": "300.0000"}]},
        ]

    def _coalesce_full_stream(self, events: list) -> list:
        """Trade-aligned coalesce — mirrors the server algorithm."""
        last_emitted: dict[tuple[str, str], Decimal] = {}
        pending: dict[tuple[str, str], Decimal] = {}
        out: list = []

        def flush(at_t: int):
            for k in sorted(pending):
                final = pending[k]
                prev = last_emitted.get(k, Decimal("0"))
                if final == prev:
                    continue
                out.append({
                    "type": "delta", "t": at_t,
                    "price": k[0], "size": str(final), "side": k[1],
                })
                last_emitted[k] = final
            pending.clear()

        last_t = 0
        for e in events:
            last_t = e["t"]
            if e["type"] == "snapshot":
                pending.clear()
                out.append(e)
                last_emitted = {}
                for level in e["bids"]:
                    last_emitted[(level["price"], "BUY")] = Decimal(level["size"])
                for level in e["asks"]:
                    last_emitted[(level["price"], "SELL")] = Decimal(level["size"])
            elif e["type"] == "delta":
                pending[(e["price"], e["side"])] = Decimal(e["size"])
            else:
                flush(e["t"])
                out.append(e)
        flush(last_t)
        return out

    def _replay_books(self, raw_events: list):
        """Run OrderBookReplay and return [(t, kind, best_bid, best_ask, depth_bid, depth_ask)]."""
        events = [_to_history_event(d) for d in raw_events]
        out = []
        for ev, book in OrderBookReplay(events, market_id="m"):
            kind = type(ev).__name__
            out.append((ev.t, kind, book.best_bid, book.best_ask, book.bid_depth, book.ask_depth))
        return out

    def test_book_at_every_trade_and_snapshot_matches(self):
        full = self._full_stream()
        compact = self._coalesce_full_stream(full)

        full_replay = self._replay_books(full)
        compact_replay = self._replay_books(compact)

        # Project each replay down to (t, best_bid, best_ask, bid_depth,
        # ask_depth) at trades and snapshots only.
        def _project(replay):
            return [
                (t, bb, ba, bd, ad)
                for (t, kind, bb, ba, bd, ad) in replay
                if kind in ("TradeEvent", "SnapshotEvent")
            ]

        assert _project(full_replay) == _project(compact_replay)
