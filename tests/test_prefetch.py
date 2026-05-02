"""Tests for PrefetchedIterator and AsyncPrefetchedIterator."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from marketlens.backtest._prefetch import (
    AsyncPrefetchedIterator,
    PrefetchedIterator,
    _TICK,
)


class TestPrefetchedIterator:
    def test_yields_in_order(self):
        items = list(range(100))
        out = list(PrefetchedIterator(iter(items)))
        assert out == items

    def test_callbacks_fire_with_first_event_and_batched_counts(self):
        items = list(range(_TICK * 2 + 5))  # 517 with TICK=256

        fetched = []

        def on_fetched(n):
            fetched.append(n)

        out = list(PrefetchedIterator(iter(items), on_fetched=on_fetched))
        assert out == items
        # batched callbacks: at least 2 ticks of 256, plus a final flush of remainder (5)
        assert sum(fetched) == len(items)
        assert _TICK in fetched

    def test_propagates_exceptions(self):
        def bad():
            yield 1
            yield 2
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            list(PrefetchedIterator(bad()))

    def test_consumer_break_does_not_leak_thread(self):
        # Slow producer: confirms stop_event is honoured promptly.
        def slow():
            for i in range(10000):
                yield i

        it = PrefetchedIterator(slow(), queue_max=4)
        gen = iter(it)
        # Pull 5 items, break.
        for _ in range(5):
            next(gen)
        gen.close()
        # The thread should finish well within the join timeout (2s).
        time.sleep(0.1)
        assert not it._thread.is_alive()

    def test_can_only_iterate_once(self):
        it = PrefetchedIterator(iter([1, 2, 3]))
        list(it)
        with pytest.raises(RuntimeError):
            list(it)

    def test_explicit_start_then_iterate(self):
        """start() before __iter__ should be idempotent and pre-fill the queue."""
        items = list(range(50))
        it = PrefetchedIterator(iter(items))
        it.start()
        # Give the worker thread a moment to push some items into the queue.
        time.sleep(0.05)
        assert it._queue.qsize() > 0  # producer ran ahead of consumer
        out = list(it)
        assert out == items

    def test_close_without_iterate_stops_thread(self):
        """A primed-but-unused prefetcher must be cleanly stoppable."""
        items = list(range(10000))
        it = PrefetchedIterator(iter(items), queue_max=8)
        it.start()
        time.sleep(0.05)  # let it start producing
        it.close()
        time.sleep(0.1)
        assert not it._thread.is_alive()

    def test_close_is_idempotent(self):
        items = [1, 2, 3]
        it = PrefetchedIterator(iter(items))
        it.start()
        it.close()
        it.close()  # should not raise


class TestCrossMarketLookahead:
    """Lookahead: prove market[i+1]'s producer can run concurrently with
    market[i]'s consumer, started by the engine before market[i] is drained."""

    def test_merge_streams_starts_all_producers_in_parallel(self):
        """Structured products: merge_streams calls next() on each stream
        eagerly, which kicks off all N PrefetchedIterators concurrently."""
        from marketlens.helpers.merge import merge_streams

        starts: list[int] = []  # records the order producer threads began work

        def _stream(idx: int):
            """A generator wrapping a PrefetchedIterator, mimicking
            _make_market_stream's shape: one prefetcher per stream, yields
            (market, event, book) tuples."""
            from marketlens.types.history import SnapshotEvent
            from marketlens.types.orderbook import OrderBook

            ev = SnapshotEvent(t=1000 + idx, is_reseed=False, bids=[], asks=[])
            book = OrderBook(
                market_id=f"m{idx}", platform="polymarket", as_of=1000 + idx,
                bids=[], asks=[], best_bid=None, best_ask=None,
                spread=None, midpoint=None, bid_depth="0", ask_depth="0",
                bid_levels=0, ask_levels=0,
            )

            def src():
                starts.append(idx)
                yield ev

            class FakeMarket:
                def __init__(self, mid):
                    self.id = mid
                    self.platform = "polymarket"

            prefetcher = PrefetchedIterator(src()).start()
            try:
                for e in prefetcher:
                    yield FakeMarket(f"m{idx}"), e, book
            finally:
                prefetcher.close()

        streams = [_stream(i) for i in range(5)]

        # Just trigger merge_streams init — it pulls first event from each stream.
        merger = merge_streams(streams)
        # Pull just enough to drive the heap init (which fires next() on every stream).
        next(merger)
        # Drain the rest so threads cleanly exit.
        list(merger)

        # All 5 producers ran. (The order isn't deterministic across threads
        # but every stream must have produced.)
        assert sorted(starts) == [0, 1, 2, 3, 4]

    def test_two_started_prefetchers_run_concurrently(self):
        # Both sources signal as soon as their producer thread starts iterating.
        p1_started = threading.Event()
        p2_started = threading.Event()

        def src1():
            p1_started.set()
            for i in range(5):
                yield i

        def src2():
            p2_started.set()
            for i in range(5):
                yield i + 100

        p1 = PrefetchedIterator(iter(src1()))
        p2 = PrefetchedIterator(iter(src2()))

        # Start both without iterating either — the engine's lookahead does this
        # for market[i] and market[i+1] simultaneously.
        p1.start()
        p2.start()

        assert p1_started.wait(timeout=1.0), "p1 producer never ran"
        assert p2_started.wait(timeout=1.0), "p2 producer ran without start()"

        # Consume them in order: both finish without blocking each other.
        assert list(p1) == list(range(5))
        assert list(p2) == list(range(100, 105))

    def test_lookahead_in_engine_consumes_two_markets(self, mock_api, client):
        """End-to-end: a 2-market series backtest produces events from both
        markets and finishes cleanly. (The lookahead pipeline is exercised
        by the engine; this test just confirms it doesn't break correctness.)"""
        from conftest import SAMPLE_MARKET, SAMPLE_SERIES
        from marketlens.backtest import Strategy
        import httpx as _httpx

        m1 = {**SAMPLE_MARKET, "id": "m1", "underlying": None,
              "series_id": SAMPLE_SERIES["id"], "open_time": 1000, "close_time": 5000}
        m2 = {**SAMPLE_MARKET, "id": "m2", "underlying": None,
              "series_id": SAMPLE_SERIES["id"], "open_time": 6000, "close_time": 10000}
        snapshot1 = {
            "type": "snapshot", "t": 1500, "is_reseed": False,
            "bids": [{"price": "0.5000", "size": "10.0000"}],
            "asks": [{"price": "0.6000", "size": "10.0000"}],
        }
        snapshot2 = {**snapshot1, "t": 6500}

        # Engine first tries the id as a market UUID — must 404 to fall through.
        mock_api.get(f"/markets/{SAMPLE_SERIES['id']}").mock(
            return_value=_httpx.Response(404, json={
                "error": {"code": "NOT_FOUND", "message": "x"},
            })
        )
        mock_api.get(f"/series/{SAMPLE_SERIES['id']}").mock(
            return_value=_httpx.Response(200, json=SAMPLE_SERIES)
        )
        mock_api.get(f"/series/{SAMPLE_SERIES['id']}/markets").mock(
            return_value=_httpx.Response(200, json={
                "data": [m1, m2], "meta": {"cursor": None, "has_more": False},
            })
        )
        mock_api.get("/markets/m1/orderbook/history").mock(
            return_value=_httpx.Response(200, json={
                "data": [snapshot1], "meta": {"cursor": None, "has_more": False},
            })
        )
        mock_api.get("/markets/m2/orderbook/history").mock(
            return_value=_httpx.Response(200, json={
                "data": [snapshot2], "meta": {"cursor": None, "has_more": False},
            })
        )

        seen: list[tuple[str, int]] = []

        class S(Strategy):
            def on_book(self, ctx, market, book):
                seen.append((market.id, book.as_of))

        result = client.backtest(
            S(), SAMPLE_SERIES["id"], initial_cash="1000",
            include_trades=False, fees=None, progress=False,
        )
        assert result is not None
        assert ("m1", 1500) in seen
        assert ("m2", 6500) in seen


class TestAsyncPrefetchedIterator:
    @pytest.mark.asyncio
    async def test_yields_in_order(self):
        async def src():
            for i in range(50):
                yield i

        out = []
        async for x in AsyncPrefetchedIterator(src()):
            out.append(x)
        assert out == list(range(50))

    @pytest.mark.asyncio
    async def test_callbacks_fire(self):
        async def src():
            for i in range(_TICK + 10):
                yield i

        fetched = []

        async for _ in AsyncPrefetchedIterator(
            src(), on_fetched=lambda n: fetched.append(n),
        ):
            pass

        assert sum(fetched) == _TICK + 10

    @pytest.mark.asyncio
    async def test_propagates_exceptions(self):
        async def bad():
            yield 1
            yield 2
            raise RuntimeError("kaboom")

        with pytest.raises(RuntimeError, match="kaboom"):
            async for _ in AsyncPrefetchedIterator(bad()):
                pass

    @pytest.mark.asyncio
    async def test_consumer_break_cancels_task(self):
        async def src():
            for i in range(10000):
                yield i

        it = AsyncPrefetchedIterator(src(), queue_max=4)
        agen = it.__aiter__()
        for _ in range(5):
            await agen.__anext__()
        await agen.aclose()
        # Producer task should be done after aclose (it propagates CancelledError).
        assert it._task is None or it._task.done()
