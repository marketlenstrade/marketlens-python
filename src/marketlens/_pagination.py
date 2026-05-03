from __future__ import annotations

from typing import Any, Generic, Iterator, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class SyncPageIterator(Generic[T]):
    """Auto-paginating synchronous iterator over API results.

    Fetches pages transparently as the caller iterates items.

    Two distinct knobs control how many items you get back:

    - ``limit`` (passed as a query param to the server) sets the **page
      size** — how many rows the server returns per request.  The iterator
      keeps fetching pages until the server reports no more results.
    - ``take`` (handled client-side, never sent to the server) sets a
      **total-item cap**.  Iteration stops after ``take`` items have been
      yielded, even if more pages remain.  Combine with a small ``limit``
      to avoid over-fetching when you only need the first few rows.
    """

    def __init__(
        self,
        client: Any,  # SyncHTTPClient
        path: str,
        params: dict[str, Any],
        model: type[T],
        data_key: str = "data",
        extra_meta_keys: tuple[str, ...] = (),
    ) -> None:
        self._client = client
        self._path = path
        self._params = dict(params)
        # `take` is a caller-side total-item cap; strip it so it isn't
        # forwarded to the server as a query param.
        self._take: int | None = self._params.pop("take", None)
        self._model = model
        self._data_key = data_key
        self._extra_meta_keys = extra_meta_keys
        self._current_page: list[T] | None = None
        self._cursor: str | None = None
        self._has_more: bool = True
        self._started = False

    def _fetch_page(self) -> None:
        if self._cursor:
            self._params["cursor"] = self._cursor
        raw = self._client.get(self._path, params=self._params)
        meta = raw.get("meta", {})
        items = raw.get(self._data_key, [])
        self._current_page = [self._model.model_validate(item) for item in items]
        self._cursor = meta.get("cursor")
        self._has_more = meta.get("has_more", False)

    def __iter__(self) -> Iterator[T]:
        self._started = True
        self._fetch_page()
        yielded = 0
        while True:
            if self._current_page:
                for item in self._current_page:
                    if self._take is not None and yielded >= self._take:
                        return
                    yield item
                    yielded += 1
            if not self._has_more:
                break
            self._fetch_page()

    def first_page(self) -> list[T]:
        """Fetch and return only the first page of results."""
        if not self._started:
            self._fetch_page()
            self._started = True
        return self._current_page or []

    def to_list(self) -> list[T]:
        """Exhaust all pages and return items as a list."""
        return list(self)

    def to_dataframe(self):  # -> pd.DataFrame
        """Convert all results to a properly-typed pandas DataFrame.

        - Decimal-string fields (prices, sizes) become ``float64``
        - Epoch-ms timestamps become ``datetime64[ns, UTC]``
        - A natural time-based index is set when one exists
        """
        from marketlens.helpers.convert import models_to_dataframe
        return models_to_dataframe(self.to_list(), self._model)


class AsyncPageIterator(Generic[T]):
    """Auto-paginating asynchronous iterator over API results.

    Two distinct knobs control how many items you get back:

    - ``limit`` (passed as a query param to the server) sets the **page
      size** — how many rows the server returns per request.  The iterator
      keeps fetching pages until the server reports no more results.
    - ``take`` (handled client-side, never sent to the server) sets a
      **total-item cap**.  Iteration stops after ``take`` items have been
      yielded, even if more pages remain.  Combine with a small ``limit``
      to avoid over-fetching when you only need the first few rows.
    """

    def __init__(
        self,
        client: Any,  # AsyncHTTPClient
        path: str,
        params: dict[str, Any],
        model: type[T],
        data_key: str = "data",
        extra_meta_keys: tuple[str, ...] = (),
    ) -> None:
        self._client = client
        self._path = path
        self._params = dict(params)
        self._take: int | None = self._params.pop("take", None)
        self._model = model
        self._data_key = data_key
        self._extra_meta_keys = extra_meta_keys
        self._current_page: list[T] | None = None
        self._cursor: str | None = None
        self._has_more: bool = True
        self._started = False

    async def _fetch_page(self) -> None:
        if self._cursor:
            self._params["cursor"] = self._cursor
        raw = await self._client.get(self._path, params=self._params)
        meta = raw.get("meta", {})
        items = raw.get(self._data_key, [])
        self._current_page = [self._model.model_validate(item) for item in items]
        self._cursor = meta.get("cursor")
        self._has_more = meta.get("has_more", False)

    async def __aiter__(self):
        self._started = True
        await self._fetch_page()
        yielded = 0
        while True:
            if self._current_page:
                for item in self._current_page:
                    if self._take is not None and yielded >= self._take:
                        return
                    yield item
                    yielded += 1
            if not self._has_more:
                break
            await self._fetch_page()

    async def first_page(self) -> list[T]:
        """Fetch and return only the first page of results."""
        if not self._started:
            await self._fetch_page()
            self._started = True
        return self._current_page or []

    async def to_list(self) -> list[T]:
        """Exhaust all pages and return items as a list."""
        items: list[T] = []
        async for item in self:
            items.append(item)
        return items

    async def to_dataframe(self):  # -> pd.DataFrame
        """Convert all results to a properly-typed pandas DataFrame.

        - Decimal-string fields (prices, sizes) become ``float64``
        - Epoch-ms timestamps become ``datetime64[ns, UTC]``
        - A natural time-based index is set when one exists
        """
        from marketlens.helpers.convert import models_to_dataframe
        items = await self.to_list()
        return models_to_dataframe(items, self._model)
