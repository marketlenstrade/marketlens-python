from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from marketlens._base import AsyncHTTPClient, SyncHTTPClient
from marketlens.exceptions import NotFoundError
from marketlens._pagination import AsyncPageIterator, SyncPageIterator
from marketlens.types.history import DeltaEvent, HistoryEvent, SnapshotEvent, TradeEvent
from marketlens.types.orderbook import BookMetrics, OrderBook

_HISTORY_EVENT_ADAPTER = TypeAdapter(HistoryEvent)


def _parse_history_event(raw: dict) -> HistoryEvent:
    return _HISTORY_EVENT_ADAPTER.validate_python(raw)


class _HistorySyncPageIterator(SyncPageIterator[HistoryEvent]):
    """Specialized iterator that discriminates history event types."""

    def _fetch_page(self) -> None:
        if self._cursor:
            self._params["cursor"] = self._cursor
        raw = self._client.get(self._path, params=self._params)
        meta = raw.get("meta", {})
        items = raw.get(self._data_key, [])
        self._current_page = [_parse_history_event(item) for item in items]
        self._cursor = meta.get("cursor")
        self._has_more = meta.get("has_more", False)


class _HistoryAsyncPageIterator(AsyncPageIterator[HistoryEvent]):
    """Specialized async iterator that discriminates history event types."""

    async def _fetch_page(self) -> None:
        if self._cursor:
            self._params["cursor"] = self._cursor
        raw = await self._client.get(self._path, params=self._params)
        meta = raw.get("meta", {})
        items = raw.get(self._data_key, [])
        self._current_page = [_parse_history_event(item) for item in items]
        self._cursor = meta.get("cursor")
        self._has_more = meta.get("has_more", False)


class Orderbook:
    def __init__(
        self, client: SyncHTTPClient, *,
        series: Any = None, markets: Any = None, events: Any = None,
    ) -> None:
        self._client = client
        self._series = series
        self._markets = markets
        self._events = events

    def get(self, market_id: str, *, at: Any = None, depth: int | None = None) -> OrderBook:
        params: dict[str, Any] = {}
        if at is not None:
            params["at"] = at
        if depth is not None:
            params["depth"] = depth
        raw = self._client.get(f"/markets/{market_id}/orderbook", params=params)
        return OrderBook.model_validate(raw)

    def history(
        self, market_id: str, *, after: Any, before: Any, **params: Any
    ) -> _HistorySyncPageIterator:
        """Stream order-book events for a market.

        Recognised kwargs:
          * ``include_trades`` — interleave trade executions in the stream.
          * ``coalesce`` — request the trade-aligned coalesced variant
            (only net inter-trade level changes are emitted; book is exact
            at every trade and snapshot).
          * ``limit`` — server-side max events per page (default 50000).
        """
        params["after"] = after
        params["before"] = before
        params.setdefault("limit", 50_000)
        return _HistorySyncPageIterator(
            self._client, f"/markets/{market_id}/orderbook/history", params, SnapshotEvent
        )

    def metrics(
        self, market_id: str, *, after: Any, before: Any, resolution: str, **params: Any
    ) -> SyncPageIterator[BookMetrics]:
        params["after"] = after
        params["before"] = before
        params["resolution"] = resolution
        return SyncPageIterator(
            self._client, f"/markets/{market_id}/orderbook/metrics", params, BookMetrics
        )

    def walk(
        self, id: str, *, after: Any = None, before: Any = None,
        data_dir: str | None = None, coalesce: bool = False, **params: Any,
    ):
        """Replay L2 books for a market, rolling series, or structured product.

        Accepts a market UUID, series slug, or Polymarket condition ID.
        Returns an iterable of ``(Market, OrderBook)`` tuples with live
        ``.books``, ``.event``, ``.series``, and ``.markets`` properties.

        Args:
            id: Market UUID, series identifier / platform slug, or condition ID.
            after: Start time (market: book history window; series: close_time filter).
            before: End time (market: book history window; series: close_time filter).
            data_dir: If set, read from local Parquet files instead of the API.
            coalesce: When True, request the compact data variant — the walk
                yields ``(market, book)`` only at trade and snapshot boundaries
                (book exact at each). Defaults to False since walk callers
                typically iterate every book transition.
            **params: Extra filter params (e.g. ``status``, ``platform``).
        """
        from marketlens.helpers.walk import EventOrderBookWalk, OrderBookWalk

        # 1. Try as a market UUID
        try:
            market = self._markets.get(id)
            series = None
            if market.series_id and self._series:
                try:
                    series = self._series.get(market.series_id)
                except Exception:
                    pass
            return OrderBookWalk(
                [market], self, after=after, before=before,
                series=series, events_resource=self._events,
                data_dir=data_dir, coalesce=coalesce,
            )
        except NotFoundError:
            pass

        # 2. Try as a series slug
        try:
            series = self._series.get(id)
            if series.structured_type:
                event_params = dict(params)
                if after is not None:
                    event_params["end_after"] = after
                if before is not None:
                    event_params["start_before"] = before
                events = self._series.events(id, **event_params).to_list()
                return EventOrderBookWalk(
                    events, self._events, self, series,
                    after=after, before=before, coalesce=coalesce,
                )
            elif series.is_rolling:
                markets = list(self._series.walk(id, after=after, before=before, **params))
                return OrderBookWalk(
                    markets, self, series=series, events_resource=self._events,
                    data_dir=data_dir, coalesce=coalesce,
                )
            else:
                raise ValueError(
                    f"Series '{series.title}' is neither rolling nor structured. "
                    f"Use a market ID to walk individual markets."
                )
        except NotFoundError:
            pass

        # 3. Fallback: try as a condition ID
        found = self._markets.list(condition_id=id).to_list()
        if found:
            market = found[0]
            series = None
            if market.series_id and self._series:
                try:
                    series = self._series.get(market.series_id)
                except Exception:
                    pass
            return OrderBookWalk(
                [market], self, after=after, before=before,
                series=series, events_resource=self._events,
                data_dir=data_dir, coalesce=coalesce,
            )

        raise NotFoundError(404, "NOT_FOUND", f"No market or series found for '{id}'")


class AsyncOrderbook:
    def __init__(
        self, client: AsyncHTTPClient, *,
        series: Any = None, markets: Any = None, events: Any = None,
    ) -> None:
        self._client = client
        self._series = series
        self._markets = markets
        self._events = events

    async def get(self, market_id: str, *, at: Any = None, depth: int | None = None) -> OrderBook:
        params: dict[str, Any] = {}
        if at is not None:
            params["at"] = at
        if depth is not None:
            params["depth"] = depth
        raw = await self._client.get(f"/markets/{market_id}/orderbook", params=params)
        return OrderBook.model_validate(raw)

    def history(
        self, market_id: str, *, after: Any, before: Any, **params: Any
    ) -> _HistoryAsyncPageIterator:
        params["after"] = after
        params["before"] = before
        params.setdefault("limit", 50_000)
        return _HistoryAsyncPageIterator(
            self._client, f"/markets/{market_id}/orderbook/history", params, SnapshotEvent
        )

    def metrics(
        self, market_id: str, *, after: Any, before: Any, resolution: str, **params: Any
    ) -> AsyncPageIterator[BookMetrics]:
        params["after"] = after
        params["before"] = before
        params["resolution"] = resolution
        return AsyncPageIterator(
            self._client, f"/markets/{market_id}/orderbook/metrics", params, BookMetrics
        )

    async def walk(
        self, id: str, *, after: Any = None, before: Any = None,
        coalesce: bool = False, **params: Any,
    ):
        """Async version of :meth:`Orderbook.walk`."""
        from marketlens.helpers.walk import AsyncEventOrderBookWalk, AsyncOrderBookWalk

        # 1. Try as a market UUID
        try:
            market = await self._markets.get(id)
            series = None
            if market.series_id and self._series:
                try:
                    series = await self._series.get(market.series_id)
                except Exception:
                    pass
            return AsyncOrderBookWalk(
                [market], self, after=after, before=before,
                series=series, events_resource=self._events,
                coalesce=coalesce,
            )
        except NotFoundError:
            pass

        # 2. Try as a series slug
        try:
            series = await self._series.get(id)
            if series.structured_type:
                event_params = dict(params)
                if after is not None:
                    event_params["end_after"] = after
                if before is not None:
                    event_params["start_before"] = before
                events = await (await self._series.events(id, **event_params)).to_list()
                return AsyncEventOrderBookWalk(
                    events, self._events, self, series,
                    after=after, before=before, coalesce=coalesce,
                )
            elif series.is_rolling:
                markets = []
                async for m in self._series.walk(id, after=after, before=before, **params):
                    markets.append(m)
                return AsyncOrderBookWalk(
                    markets, self, series=series, events_resource=self._events,
                    coalesce=coalesce,
                )
            else:
                raise ValueError(
                    f"Series '{series.title}' is neither rolling nor structured. "
                    f"Use a market ID to walk individual markets."
                )
        except NotFoundError:
            pass

        # 3. Fallback: try as a condition ID
        found = await self._markets.list(condition_id=id).to_list()
        if found:
            market = found[0]
            series = None
            if market.series_id and self._series:
                try:
                    series = await self._series.get(market.series_id)
                except Exception:
                    pass
            return AsyncOrderBookWalk(
                [market], self, after=after, before=before,
                series=series, events_resource=self._events,
                coalesce=coalesce,
            )

        raise NotFoundError(404, "NOT_FOUND", f"No market or series found for '{id}'")
