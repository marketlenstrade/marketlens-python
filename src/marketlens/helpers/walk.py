"""Order book walk — replay L2 books for a single market or across a series.

Single-market replay::

    for market, book in client.orderbook.walk(market_id, after=start, before=end):
        print(book.midpoint, book.spread_bps())

Rolling series replay::

    for market, book in client.orderbook.walk("btc-up-or-down-5m", status="resolved"):
        print(market.question, book.imbalance())

Structured product replay::

    walk = client.orderbook.walk("btc-multi-strikes-weekly")
    for market, book in walk:
        surface = walk.surface()
        print(f"{market.question}: mid={book.midpoint}, implied_mean={surface.implied_mean}")

All support ``.to_dataframe()``::

    df = client.orderbook.walk(market_id, after=start, before=end).to_dataframe()
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterator

from marketlens._base import _coerce_timestamp
from marketlens.helpers.merge import async_merge_replays, merge_replays
from marketlens.helpers.replay import (
    AsyncOrderBookReplay,
    OrderBookReplay,
    _book_to_row,
    _rows_to_dataframe,
)
from marketlens.helpers.surface import compute_surface
from marketlens.types.event import Event
from marketlens.types.history import TradeEvent
from marketlens.types.market import Market
from marketlens.types.orderbook import OrderBook
from marketlens.types.series import Series
from marketlens.types.signal import Surface


# ── Sequential walk (single market / rolling series) ─────────────


class OrderBookWalk:
    """Iterate ``(Market, OrderBook)`` tuples for one or more markets.

    Properties updated during iteration:

    - ``books`` — ``{market_id: OrderBook}`` for the current market
    - ``event`` — resolved ``Event`` (best-effort, may be None)
    - ``series`` — resolved ``Series`` (if available)
    - ``markets`` — ``{market_id: Market}`` for the current context
    """

    def __init__(
        self, markets: list[Market], orderbook_resource: Any,
        *, after: Any = None, before: Any = None,
        series: Series | None = None, events_resource: Any = None,
        data_dir: str | None = None,
    ) -> None:
        self._markets_list = markets
        self._orderbook = orderbook_resource
        self._after = after
        self._before = before
        self._series_obj = series
        self._events_resource = events_resource
        self._event_cache: dict[str, Event | None] = {}
        self._data_dir = data_dir

        self._books: dict[str, OrderBook] = {}
        self._current_event: Event | None = None
        self._current_markets: dict[str, Market] = {}

    @property
    def books(self) -> dict[str, OrderBook]:
        return dict(self._books)

    @property
    def event(self) -> Event | None:
        return self._current_event

    @property
    def series(self) -> Series | None:
        return self._series_obj

    @property
    def markets(self) -> dict[str, Market]:
        return dict(self._current_markets)

    def surface(self) -> Surface | None:
        """Compute implied probability surface from current book state.

        Returns ``None`` for non-structured series or insufficient data.
        """
        if not self._series_obj:
            return None
        return compute_surface(
            self._books, self._current_markets,
            self._series_obj, self._current_event,
        )

    def _resolve_event(self, market: Market) -> Event | None:
        eid = market.event_id
        if eid not in self._event_cache:
            if self._events_resource is None:
                self._event_cache[eid] = None
            else:
                try:
                    self._event_cache[eid] = self._events_resource.get(eid)
                except Exception:
                    self._event_cache[eid] = None
        return self._event_cache[eid]

    def __iter__(self) -> Iterator[tuple[Market, OrderBook]]:
        from pathlib import Path

        for market in self._markets_list:
            self._current_markets = {market.id: market}
            self._books = {}
            self._current_event = self._resolve_event(market)

            if self._data_dir:
                path = Path(self._data_dir) / f"history-{market.id}.parquet"
                if path.exists():
                    from marketlens.backtest._engine import _iter_history_parquet
                    events = _iter_history_parquet(path)
                else:
                    import warnings
                    warnings.warn(f"Skipping {market.id}: {path} not found")
                    continue
            else:
                events = self._orderbook.history(
                    market.id,
                    after=self._after or market.open_time,
                    before=self._before or market.close_time,
                )

            replay = OrderBookReplay(
                events, market_id=market.id, platform=market.platform,
            )
            for event, book in replay:
                if not isinstance(event, TradeEvent):
                    self._books[market.id] = book
                    yield market, book

    def to_dataframe(self):
        """Replay all markets and return a DataFrame."""
        rows: list[dict] = []
        for market, book in self:
            row = _book_to_row(book)
            row["t"] = book.as_of
            row["market_id"] = market.id
            row["winning_outcome"] = market.winning_outcome
            rows.append(row)
        return _rows_to_dataframe(rows)


class AsyncOrderBookWalk:
    """Async version of :class:`OrderBookWalk`."""

    def __init__(
        self, markets: list[Market], orderbook_resource: Any,
        *, after: Any = None, before: Any = None,
        series: Series | None = None, events_resource: Any = None,
        data_dir: str | None = None,
    ) -> None:
        self._markets_list = markets
        self._orderbook = orderbook_resource
        self._after = after
        self._before = before
        self._series_obj = series
        self._events_resource = events_resource
        self._event_cache: dict[str, Event | None] = {}
        self._data_dir = data_dir

        self._books: dict[str, OrderBook] = {}
        self._current_event: Event | None = None
        self._current_markets: dict[str, Market] = {}

    @property
    def books(self) -> dict[str, OrderBook]:
        return dict(self._books)

    @property
    def event(self) -> Event | None:
        return self._current_event

    @property
    def series(self) -> Series | None:
        return self._series_obj

    @property
    def markets(self) -> dict[str, Market]:
        return dict(self._current_markets)

    def surface(self) -> Surface | None:
        if not self._series_obj:
            return None
        return compute_surface(
            self._books, self._current_markets,
            self._series_obj, self._current_event,
        )

    async def _resolve_event(self, market: Market) -> Event | None:
        eid = market.event_id
        if eid not in self._event_cache:
            if self._events_resource is None:
                self._event_cache[eid] = None
            else:
                try:
                    self._event_cache[eid] = await self._events_resource.get(eid)
                except Exception:
                    self._event_cache[eid] = None
        return self._event_cache[eid]

    async def __aiter__(self) -> AsyncIterator[tuple[Market, OrderBook]]:
        for market in self._markets_list:
            self._current_markets = {market.id: market}
            self._books = {}
            self._current_event = await self._resolve_event(market)

            history = self._orderbook.history(
                market.id,
                after=self._after or market.open_time,
                before=self._before or market.close_time,
            )
            replay = AsyncOrderBookReplay(
                history, market_id=market.id, platform=market.platform,
            )
            async for event, book in replay:
                if not isinstance(event, TradeEvent):
                    self._books[market.id] = book
                    yield market, book

    async def to_dataframe(self):
        rows: list[dict] = []
        async for market, book in self:
            row = _book_to_row(book)
            row["t"] = book.as_of
            row["market_id"] = market.id
            row["winning_outcome"] = market.winning_outcome
            rows.append(row)
        return _rows_to_dataframe(rows)


# ── Event-level walk (structured products) ───────────────────────


class EventOrderBookWalk:
    """Walk a structured series, merging all strike markets per event.

    Each event's markets are replayed in parallel via heap-merge so that
    ``books`` always reflects the latest state of every sibling strike.
    Call ``surface()`` at any point during iteration to get the implied
    probability surface from current book state.
    """

    def __init__(
        self, events: list[Event], events_resource: Any,
        orderbook_resource: Any, series: Series,
        *, after: Any = None, before: Any = None,
    ) -> None:
        self._events_list = events
        self._events_resource = events_resource
        self._orderbook = orderbook_resource
        self._series_obj = series
        self._after = after
        self._before = before

        self._books: dict[str, OrderBook] = {}
        self._current_event: Event | None = None
        self._current_markets: dict[str, Market] = {}

    @property
    def books(self) -> dict[str, OrderBook]:
        return dict(self._books)

    @property
    def event(self) -> Event | None:
        return self._current_event

    @property
    def series(self) -> Series:
        return self._series_obj

    @property
    def markets(self) -> dict[str, Market]:
        return dict(self._current_markets)

    def surface(self) -> Surface | None:
        """Compute implied probability surface from current book state."""
        return compute_surface(
            self._books, self._current_markets,
            self._series_obj, self._current_event,
        )

    @staticmethod
    def _event_overlaps(evt: Event, after_ms: int | None, before_ms: int | None) -> bool:
        """Check if event's time range overlaps [after, before]."""
        if after_ms and evt.end_date and evt.end_date < after_ms:
            return False
        if before_ms and evt.start_date and evt.start_date > before_ms:
            return False
        return True

    def __iter__(self) -> Iterator[tuple[Market, OrderBook]]:
        after_ms = _coerce_timestamp(self._after) if self._after else None
        before_ms = _coerce_timestamp(self._before) if self._before else None

        for evt in self._events_list:
            if not self._event_overlaps(evt, after_ms, before_ms):
                continue
            self._current_event = evt
            self._books = {}

            event_markets = self._events_resource.markets(evt.id).to_list()
            self._current_markets = {m.id: m for m in event_markets}

            replays: list[tuple[Market, OrderBookReplay]] = []
            for m in event_markets:
                history = self._orderbook.history(
                    m.id,
                    after=self._after or m.open_time,
                    before=self._before or m.close_time,
                )
                replays.append((
                    m,
                    OrderBookReplay(history, market_id=m.id, platform=m.platform),
                ))

            for market, event, book in merge_replays(replays):
                if not isinstance(event, TradeEvent):
                    self._books[market.id] = book
                    yield market, book

    def to_dataframe(self):
        """Interleaved DataFrame with ``market_id`` and ``event_id`` columns."""
        rows: list[dict] = []
        for market, book in self:
            row = _book_to_row(book)
            row["t"] = book.as_of
            row["market_id"] = market.id
            row["event_id"] = self._current_event.id if self._current_event else None
            row["winning_outcome"] = market.winning_outcome
            rows.append(row)
        return _rows_to_dataframe(rows)


class AsyncEventOrderBookWalk:
    """Async version of :class:`EventOrderBookWalk`."""

    def __init__(
        self, events: list[Event], events_resource: Any,
        orderbook_resource: Any, series: Series,
        *, after: Any = None, before: Any = None,
    ) -> None:
        self._events_list = events
        self._events_resource = events_resource
        self._orderbook = orderbook_resource
        self._series_obj = series
        self._after = after
        self._before = before

        self._books: dict[str, OrderBook] = {}
        self._current_event: Event | None = None
        self._current_markets: dict[str, Market] = {}

    @property
    def books(self) -> dict[str, OrderBook]:
        return dict(self._books)

    @property
    def event(self) -> Event | None:
        return self._current_event

    @property
    def series(self) -> Series:
        return self._series_obj

    @property
    def markets(self) -> dict[str, Market]:
        return dict(self._current_markets)

    def surface(self) -> Surface | None:
        return compute_surface(
            self._books, self._current_markets,
            self._series_obj, self._current_event,
        )

    async def __aiter__(self) -> AsyncIterator[tuple[Market, OrderBook]]:
        after_ms = _coerce_timestamp(self._after) if self._after else None
        before_ms = _coerce_timestamp(self._before) if self._before else None

        for evt in self._events_list:
            if not EventOrderBookWalk._event_overlaps(evt, after_ms, before_ms):
                continue
            self._current_event = evt
            self._books = {}

            event_markets = await self._events_resource.markets(evt.id).to_list()
            self._current_markets = {m.id: m for m in event_markets}

            replays: list[tuple[Market, AsyncOrderBookReplay]] = []
            for m in event_markets:
                history = self._orderbook.history(
                    m.id,
                    after=self._after or m.open_time,
                    before=self._before or m.close_time,
                )
                replays.append((
                    m,
                    AsyncOrderBookReplay(history, market_id=m.id, platform=m.platform),
                ))

            async for market, event, book in async_merge_replays(replays):
                if not isinstance(event, TradeEvent):
                    self._books[market.id] = book
                    yield market, book

    async def to_dataframe(self):
        rows: list[dict] = []
        async for market, book in self:
            row = _book_to_row(book)
            row["t"] = book.as_of
            row["market_id"] = market.id
            row["event_id"] = self._current_event.id if self._current_event else None
            row["winning_outcome"] = market.winning_outcome
            rows.append(row)
        return _rows_to_dataframe(rows)
