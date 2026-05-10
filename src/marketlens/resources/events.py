from __future__ import annotations

from typing import Any

from marketlens._base import (
    AsyncHTTPClient,
    SyncHTTPClient,
    _coerce_timestamp_params,
)
from marketlens._pagination import AsyncPageIterator, SyncPageIterator
from marketlens.types.event import Event
from marketlens.types.market import Market


class Events:
    def __init__(self, client: SyncHTTPClient) -> None:
        self._client = client

    def list(self, **params: Any) -> SyncPageIterator[Event]:
        return SyncPageIterator(
            self._client, "/events", _coerce_timestamp_params(params), Event,
        )

    def get(self, event_id: str) -> Event:
        raw = self._client.get(f"/events/{event_id}")
        return Event.model_validate(raw)

    def markets(self, event_id: str, **params: Any) -> SyncPageIterator[Market]:
        return SyncPageIterator(
            self._client, f"/events/{event_id}/markets",
            _coerce_timestamp_params(params), Market,
        )


class AsyncEvents:
    def __init__(self, client: AsyncHTTPClient) -> None:
        self._client = client

    def list(self, **params: Any) -> AsyncPageIterator[Event]:
        return AsyncPageIterator(
            self._client, "/events", _coerce_timestamp_params(params), Event,
        )

    async def get(self, event_id: str) -> Event:
        raw = await self._client.get(f"/events/{event_id}")
        return Event.model_validate(raw)

    def markets(self, event_id: str, **params: Any) -> AsyncPageIterator[Market]:
        return AsyncPageIterator(
            self._client, f"/events/{event_id}/markets",
            _coerce_timestamp_params(params), Market,
        )
