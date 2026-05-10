from __future__ import annotations

from typing import Any

from marketlens._base import (
    AsyncHTTPClient,
    SyncHTTPClient,
    _coerce_timestamp_params,
)
from marketlens._pagination import AsyncPageIterator, SyncPageIterator
from marketlens.types.reference import ReferenceCandle, ReferenceTrade


class Reference:
    def __init__(self, client: SyncHTTPClient) -> None:
        self._client = client

    def candles(self, symbol: str, **params: Any) -> SyncPageIterator[ReferenceCandle]:
        params["symbol"] = symbol
        return SyncPageIterator(
            self._client, "/reference/candles",
            _coerce_timestamp_params(params), ReferenceCandle,
        )

    def trades(self, symbol: str, **params: Any) -> SyncPageIterator[ReferenceTrade]:
        params["symbol"] = symbol
        return SyncPageIterator(
            self._client, "/reference/trades",
            _coerce_timestamp_params(params), ReferenceTrade,
        )


class AsyncReference:
    def __init__(self, client: AsyncHTTPClient) -> None:
        self._client = client

    def candles(self, symbol: str, **params: Any) -> AsyncPageIterator[ReferenceCandle]:
        params["symbol"] = symbol
        return AsyncPageIterator(
            self._client, "/reference/candles",
            _coerce_timestamp_params(params), ReferenceCandle,
        )

    def trades(self, symbol: str, **params: Any) -> AsyncPageIterator[ReferenceTrade]:
        params["symbol"] = symbol
        return AsyncPageIterator(
            self._client, "/reference/trades",
            _coerce_timestamp_params(params), ReferenceTrade,
        )
