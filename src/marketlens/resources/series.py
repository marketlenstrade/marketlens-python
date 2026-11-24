from __future__ import annotations

from typing import Any, Iterator

from marketlens._base import (
    AsyncHTTPClient,
    SyncHTTPClient,
    _coerce_timestamp,
    _coerce_timestamp_params,
)
from marketlens._pagination import AsyncPageIterator, SyncPageIterator
from marketlens.exceptions import NotFoundError
from marketlens.types.event import Event
from marketlens.types.market import Market
from marketlens.types.series import Series


class SeriesResource:
    def __init__(self, client: SyncHTTPClient) -> None:
        self._client = client

    def _resolve(self, series_id: str) -> Series:
        """Resolve *series_id* to a Series, accepting platform slugs."""
        try:
            raw = self._client.get(f"/series/{series_id}")
            return Series.model_validate(raw)
        except NotFoundError:
            pass
        # Fall back to searching by platform_series_id (exact match client-side)
        for s in SyncPageIterator(
            self._client, "/series", {"platform_series_id": series_id}, Series,
        ):
            if s.platform_series_id == series_id:
                return s
        raise NotFoundError(404, "SERIES_NOT_FOUND", f"Series {series_id} not found")

    def list(self, **params: Any) -> SyncPageIterator[Series]:
        return SyncPageIterator(
            self._client, "/series", _coerce_timestamp_params(params), Series,
        )

    def get(self, series_id: str) -> Series:
        return self._resolve(series_id)

    def markets(self, series_id: str, **params: Any) -> SyncPageIterator[Market]:
        resolved = self._resolve(series_id).id
        return SyncPageIterator(
            self._client, f"/series/{resolved}/markets",
            _coerce_timestamp_params(params), Market,
        )

    def walk(
        self, series_id: str, *, after: Any = None, before: Any = None, **params: Any,
    ) -> Iterator[Market]:
        """Iterate markets in a series chronologically over a half-open window.

        Markets are sorted by ``close_time`` ascending (earliest first).
        Pass ``status="resolved"`` to only walk completed markets.

        The ``(after, before)`` window is half-open ``[after, before)``: a
        market with ``close_time == after`` or ``open_time == before`` is
        excluded. This matches the set returned by
        ``client.exports.download_series(after=..., before=...)`` so the same
        backtest produces the same market set whether streamed or replayed
        from a local export.

        Usage::

            for market in client.series.walk("btc-up-or-down-5m", status="resolved"):
                ...

        Args:
            series_id: Series identifier.
            after: Half-open lower bound — only include markets whose lifetime
                strictly extends past this time (``close_time > after``).
            before: Half-open upper bound — only include markets whose lifetime
                strictly starts before this time (``open_time < before``).
            **params: Extra filter params (e.g. ``status``, ``platform``).
        """
        resolved = self._resolve(series_id)
        if not resolved.is_rolling:
            raise ValueError(
                f"series.walk() is only supported for rolling series. "
                f"'{resolved.title}' is not rolling. Use series.events() to browse its events."
            )
        yield from self._markets_in_window(resolved.id, after=after, before=before, **params)

    def _markets_in_window(
        self, series_uuid: str, *, after: Any = None, before: Any = None,
        inclusive: bool = False, **params: Any,
    ) -> Iterator[Market]:
        """Markets of a series whose life overlaps ``[after, before)``, by close_time.

        ``inclusive=True`` also keeps a market with ``close_time == after`` or
        ``open_time == before``: a structured market can hold books at its
        listed close, so those paths keep the boundary market.
        """
        params["sort"] = "close_time"
        edge = 0 if inclusive else 1

        if after is not None or before is not None:
            params["series_id"] = series_uuid
            # The server's bounds are inclusive. Half-open: close_time >= after+1
            # iff close_time > after; open_time <= before-1 iff open_time < before.
            # Timestamps are integer ms so the +1/-1 is exact.
            if after is not None:
                params["close_after"] = _coerce_timestamp(after) + edge
            if before is not None:
                params["open_before"] = _coerce_timestamp(before) - edge
            yield from SyncPageIterator(
                self._client, "/markets",
                _coerce_timestamp_params(params), Market,
            )
        else:
            yield from SyncPageIterator(
                self._client, f"/series/{series_uuid}/markets",
                _coerce_timestamp_params(params), Market,
            )

    def events(self, series_id: str, **params: Any) -> SyncPageIterator[Event]:
        """List events belonging to a series.

        Useful for non-rolling series where markets are grouped by event
        (e.g. weekly strike groups). Returns events sorted by ``end_date``.

        Args:
            series_id: Series identifier or platform slug.
            **params: Extra filter params.
        """
        resolved = self._resolve(series_id)
        params.setdefault("sort", "end_date")
        params["series_id"] = resolved.id
        return SyncPageIterator(
            self._client, "/events", _coerce_timestamp_params(params), Event,
        )


class AsyncSeriesResource:
    def __init__(self, client: AsyncHTTPClient) -> None:
        self._client = client

    async def _resolve(self, series_id: str) -> Series:
        """Resolve *series_id* to a Series, accepting platform slugs."""
        try:
            raw = await self._client.get(f"/series/{series_id}")
            return Series.model_validate(raw)
        except NotFoundError:
            pass
        async for s in AsyncPageIterator(
            self._client, "/series", {"platform_series_id": series_id}, Series,
        ):
            if s.platform_series_id == series_id:
                return s
        raise NotFoundError(404, "SERIES_NOT_FOUND", f"Series {series_id} not found")

    def list(self, **params: Any) -> AsyncPageIterator[Series]:
        return AsyncPageIterator(
            self._client, "/series", _coerce_timestamp_params(params), Series,
        )

    async def get(self, series_id: str) -> Series:
        return await self._resolve(series_id)

    async def markets(self, series_id: str, **params: Any) -> AsyncPageIterator[Market]:
        resolved = (await self._resolve(series_id)).id
        return AsyncPageIterator(
            self._client, f"/series/{resolved}/markets",
            _coerce_timestamp_params(params), Market,
        )

    async def walk(
        self, series_id: str, *, after: Any = None, before: Any = None, **params: Any,
    ):
        """Async version of :meth:`SeriesResource.walk`.

        The ``(after, before)`` window is half-open ``[after, before)`` — see
        the sync version for the rationale and details.

        Usage::

            async for market in client.series.walk("btc-5min-rolling"):
                print(market.question)
        """
        resolved = await self._resolve(series_id)
        if not resolved.is_rolling:
            raise ValueError(
                f"series.walk() is only supported for rolling series. "
                f"'{resolved.title}' is not rolling. Use series.events() to browse its events."
            )
        async for market in self._markets_in_window(
            resolved.id, after=after, before=before, **params,
        ):
            yield market

    async def _markets_in_window(
        self, series_uuid: str, *, after: Any = None, before: Any = None,
        inclusive: bool = False, **params: Any,
    ):
        """Async version of :meth:`SeriesResource._markets_in_window`."""
        params["sort"] = "close_time"
        edge = 0 if inclusive else 1

        if after is not None or before is not None:
            params["series_id"] = series_uuid
            if after is not None:
                params["close_after"] = _coerce_timestamp(after) + edge
            if before is not None:
                params["open_before"] = _coerce_timestamp(before) - edge
            async for market in AsyncPageIterator(
                self._client, "/markets",
                _coerce_timestamp_params(params), Market,
            ):
                yield market
        else:
            async for market in AsyncPageIterator(
                self._client, f"/series/{series_uuid}/markets",
                _coerce_timestamp_params(params), Market,
            ):
                yield market

    async def events(self, series_id: str, **params: Any) -> AsyncPageIterator[Event]:
        """List events belonging to a series (async).

        Args:
            series_id: Series identifier or platform slug.
            **params: Extra filter params.
        """
        resolved = await self._resolve(series_id)
        params.setdefault("sort", "end_date")
        params["series_id"] = resolved.id
        return AsyncPageIterator(
            self._client, "/events", _coerce_timestamp_params(params), Event,
        )
