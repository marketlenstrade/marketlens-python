"""Bounded producer/consumer iterator that hides page-fetch latency.

A consumer iterating ``PrefetchedIterator(source)`` gets the same items as
iterating ``source`` directly, but a background worker reads ahead and
buffers up to ``queue_max`` items. Network round-trips for the next page
overlap with the engine's processing of the current page.

Backpressure: when the queue fills, the worker blocks on put(), which
naturally throttles the upstream HTTP iterator so we never burst more
than ``queue_max // page_size`` pages ahead.

Shutdown: a stop event lets the worker exit promptly when the consumer
breaks early or raises. Exceptions raised inside the worker are
propagated to the consumer on the next ``next()`` call.
"""
from __future__ import annotations

import asyncio
import queue
import threading
from typing import (
    AsyncIterable,
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
    TypeVar,
)

T = TypeVar("T")

# Sentinels. Object identity, never compared by value.
_DONE = object()
_ERROR = object()

# Coalesce on_fetched callbacks to keep the rich render loop responsive
# even when the producer pushes events much faster than the consumer reads.
_TICK = 256

# Queue size in events. Sized to roughly one page of history so the
# producer is fully pipelined with the consumer (network and processing
# overlap) without holding excessive memory per concurrent stream.
_DEFAULT_QUEUE_MAX = 10000


class PrefetchedIterator(Iterable[T]):
    """Sync prefetch wrapper.

    Args:
        source: The underlying iterable to drain in a worker thread.
        queue_max: Maximum number of items buffered ahead of the consumer.
        on_fetched: Optional callback ``(n_added)`` invoked roughly every
            ``_TICK`` items pushed.
        on_done: Optional callback invoked once after the source is fully
            drained (before ``_DONE`` is posted to the queue).
    """

    def __init__(
        self,
        source: Iterable[T],
        *,
        queue_max: int = _DEFAULT_QUEUE_MAX,
        on_fetched: Callable[[int], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self._source = source
        self._queue: queue.Queue = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._on_fetched = on_fetched
        self._on_done = on_done
        self._thread = threading.Thread(
            target=self._run, name="marketlens-prefetch", daemon=True,
        )
        self._thread_started = False
        self._consumed = False
        self._closed = False

    def _run(self) -> None:
        pending = 0
        try:
            for item in self._source:
                # Short timeout on put() so a stop request is noticed
                # promptly even when the consumer has stalled.
                while not self._stop.is_set():
                    try:
                        self._queue.put(item, timeout=0.5)
                        break
                    except queue.Full:
                        continue
                else:
                    return  # stop requested
                pending += 1
                if pending >= _TICK and self._on_fetched is not None:
                    try:
                        self._on_fetched(pending)
                    except Exception:
                        pass
                    pending = 0
        except BaseException as exc:  # propagate to consumer
            self._queue.put((_ERROR, exc))
            return
        if pending and self._on_fetched is not None:
            try:
                self._on_fetched(pending)
            except Exception:
                pass
        if self._on_done is not None:
            try:
                self._on_done()
            except Exception:
                pass
        self._queue.put(_DONE)

    def start(self) -> "PrefetchedIterator[T]":
        """Start the producer thread. Idempotent. Returns self. Call
        explicitly to prime a prefetcher before iteration begins."""
        if not self._thread_started:
            self._thread_started = True
            self._thread.start()
        return self

    def close(self) -> None:
        """Stop the producer thread and reclaim resources. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread_started:
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass
            self._thread.join(timeout=2.0)

    def __iter__(self) -> Iterator[T]:
        if self._consumed:
            raise RuntimeError("PrefetchedIterator can only be iterated once")
        self._consumed = True
        self.start()
        try:
            while True:
                item = self._queue.get()
                if item is _DONE:
                    return
                if isinstance(item, tuple) and len(item) == 2 and item[0] is _ERROR:
                    raise item[1]
                yield item
        finally:
            self.close()


class AsyncPrefetchedIterator(AsyncIterable[T]):
    """Async prefetch wrapper.

    Spawns an ``asyncio.Task`` to drain ``source`` into a bounded
    ``asyncio.Queue``. Same shape as :class:`PrefetchedIterator`.
    """

    def __init__(
        self,
        source: AsyncIterable[T],
        *,
        queue_max: int = _DEFAULT_QUEUE_MAX,
        on_fetched: Callable[[int], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self._source = source
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max)
        self._on_fetched = on_fetched
        self._on_done = on_done
        self._task: asyncio.Task | None = None
        self._consumed = False
        self._closed = False

    async def _run(self) -> None:
        pending = 0
        try:
            async for item in self._source:
                await self._queue.put(item)
                pending += 1
                if pending >= _TICK and self._on_fetched is not None:
                    try:
                        self._on_fetched(pending)
                    except Exception:
                        pass
                    pending = 0
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._queue.put((_ERROR, exc))
            return
        if pending and self._on_fetched is not None:
            try:
                self._on_fetched(pending)
            except Exception:
                pass
        if self._on_done is not None:
            try:
                self._on_done()
            except Exception:
                pass
        await self._queue.put(_DONE)

    def start(self) -> "AsyncPrefetchedIterator[T]":
        """Start the producer task. Idempotent. Returns self.

        Must be called from within a running event loop.
        """
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        return self

    async def close(self) -> None:
        """Cancel the producer task. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, BaseException):
                pass

    async def __aiter__(self) -> AsyncIterator[T]:
        if self._consumed:
            raise RuntimeError("AsyncPrefetchedIterator can only be iterated once")
        self._consumed = True
        self.start()
        try:
            while True:
                item = await self._queue.get()
                if item is _DONE:
                    return
                if isinstance(item, tuple) and len(item) == 2 and item[0] is _ERROR:
                    raise item[1]
                yield item
        finally:
            await self.close()
