from __future__ import annotations

from typing import Any

from marketlens._base import (
    AsyncHTTPClient,
    SyncHTTPClient,
    _coerce_timestamp_params,
)
from marketlens._pagination import AsyncPageIterator, SyncPageIterator
from marketlens.types.signal import Surface


class Signals:
    def __init__(self, client: SyncHTTPClient) -> None:
        self._client = client

    def surfaces(self, **params: Any) -> SyncPageIterator[Surface]:
        """List latest implied probability surfaces.

        Params: underlying, surface_type
        """
        return SyncPageIterator(
            self._client, "/signals/surfaces",
            _coerce_timestamp_params(params), Surface,
        )

    def surface(self, series_id: str, event_id: str) -> Surface:
        """Get latest surface for a specific series/event."""
        raw = self._client.get(f"/signals/surfaces/{series_id}/{event_id}")
        return Surface.model_validate(raw)

    def history(
        self, series_id: str, event_id: str, **params: Any,
    ) -> SyncPageIterator[Surface]:
        """Time series of implied stats for a surface.

        Params: limit, after, before, order
        """
        return SyncPageIterator(
            self._client,
            f"/signals/surfaces/{series_id}/{event_id}/history",
            _coerce_timestamp_params(params),
            Surface,
        )


class AsyncSignals:
    def __init__(self, client: AsyncHTTPClient) -> None:
        self._client = client

    def surfaces(self, **params: Any) -> AsyncPageIterator[Surface]:
        """List latest implied probability surfaces.

        Params: underlying, surface_type
        """
        return AsyncPageIterator(
            self._client, "/signals/surfaces",
            _coerce_timestamp_params(params), Surface,
        )

    async def surface(self, series_id: str, event_id: str) -> Surface:
        """Get latest surface for a specific series/event."""
        raw = await self._client.get(f"/signals/surfaces/{series_id}/{event_id}")
        return Surface.model_validate(raw)

    def history(
        self, series_id: str, event_id: str, **params: Any,
    ) -> AsyncPageIterator[Surface]:
        """Time series of implied stats for a surface.

        Params: limit, after, before, order
        """
        return AsyncPageIterator(
            self._client,
            f"/signals/surfaces/{series_id}/{event_id}/history",
            _coerce_timestamp_params(params),
            Surface,
        )
