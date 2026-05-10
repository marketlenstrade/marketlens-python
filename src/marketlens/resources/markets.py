from __future__ import annotations

from typing import Any

from marketlens._base import (
    AsyncHTTPClient,
    SyncHTTPClient,
    _coerce_timestamp_params,
)
from marketlens._pagination import AsyncPageIterator, SyncPageIterator
from marketlens.types.candle import Candle
from marketlens.types.market import Market
from marketlens.types.trade import Trade


class Markets:
    def __init__(self, client: SyncHTTPClient) -> None:
        self._client = client

    def list(self, **params: Any) -> SyncPageIterator[Market]:
        return SyncPageIterator(
            self._client, "/markets", _coerce_timestamp_params(params), Market,
        )

    def get(self, market_id: str) -> Market:
        raw = self._client.get(f"/markets/{market_id}")
        return Market.model_validate(raw)

    def trades(self, market_id: str, **params: Any) -> SyncPageIterator[Trade]:
        return SyncPageIterator(
            self._client, f"/markets/{market_id}/trades",
            _coerce_timestamp_params(params), Trade,
        )

    def candles(self, market_id: str, **params: Any) -> SyncPageIterator[Candle]:
        return SyncPageIterator(
            self._client, f"/markets/{market_id}/candles",
            _coerce_timestamp_params(params), Candle,
        )


class AsyncMarkets:
    def __init__(self, client: AsyncHTTPClient) -> None:
        self._client = client

    def list(self, **params: Any) -> AsyncPageIterator[Market]:
        return AsyncPageIterator(
            self._client, "/markets", _coerce_timestamp_params(params), Market,
        )

    async def get(self, market_id: str) -> Market:
        raw = await self._client.get(f"/markets/{market_id}")
        return Market.model_validate(raw)

    def trades(self, market_id: str, **params: Any) -> AsyncPageIterator[Trade]:
        return AsyncPageIterator(
            self._client, f"/markets/{market_id}/trades",
            _coerce_timestamp_params(params), Trade,
        )

    def candles(self, market_id: str, **params: Any) -> AsyncPageIterator[Candle]:
        return AsyncPageIterator(
            self._client, f"/markets/{market_id}/candles",
            _coerce_timestamp_params(params), Candle,
        )
