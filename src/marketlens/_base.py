from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from marketlens._constants import DEFAULT_BASE_URL, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT, VERSION
from marketlens.exceptions import (
    APIError,
    ConnectionError,
    RateLimitError,
    TimeoutError,
    _CODE_TO_EXCEPTION,
    _STATUS_TO_EXCEPTION,
)


def _coerce_timestamp(value: Any) -> Any:
    """Coerce datetime / numeric str / ISO 8601 str to ms epoch. Pass through ints / None."""
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, str):
        s = value.strip()
        if s.lstrip("-").isdigit():
            return int(s)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            return value
    return value


def _prepare_params(params: dict[str, Any]) -> dict[str, Any]:
    """Clean None values and coerce timestamps."""
    out: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        v = _coerce_timestamp(v)
        if isinstance(v, bool):
            out[k] = str(v).lower()
        else:
            out[k] = v
    return out


def _raise_for_error(response: httpx.Response) -> None:
    """Parse API error JSON and raise the appropriate exception."""
    if response.status_code < 400:
        return

    try:
        body = response.json()
        error = body.get("error", {})
        code = error.get("code", str(response.status_code))
        message = error.get("message", response.text)
    except Exception:
        code = str(response.status_code)
        message = response.text

    # Pick exception class: prefer code-based mapping, fall back to status
    exc_cls = _CODE_TO_EXCEPTION.get(code) or _STATUS_TO_EXCEPTION.get(response.status_code, APIError)

    if exc_cls is RateLimitError:
        retry_after_raw = response.headers.get("Retry-After")
        retry_after = int(retry_after_raw) if retry_after_raw else None
        raise RateLimitError(response.status_code, code, message, retry_after=retry_after)

    raise exc_cls(response.status_code, code, message)


def _should_retry(response: httpx.Response) -> bool:
    return response.status_code == 429 or response.status_code >= 500


def _user_agent() -> str:
    return f"marketlens-python/{VERSION}"


class SyncHTTPClient:
    """Synchronous HTTP transport with auth, retry, and error mapping."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.api_key = api_key or os.environ.get("MARKETLENS_API_KEY", "")
        self.base_url = (os.environ.get("MARKETLENS_BASE_URL") or base_url).rstrip("/")
        self.max_retries = max_retries
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "User-Agent": _user_agent(),
                "Authorization": f"Bearer {self.api_key}",
            },
        )

    def _request_with_retry(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a request with retry logic. Returns the raw response."""
        last_exc: Exception | None = None
        for attempt in range(1 + self.max_retries):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                last_exc = TimeoutError(str(exc))
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise last_exc from exc
            except httpx.ConnectError as exc:
                last_exc = ConnectionError(str(exc))
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise last_exc from exc

            if _should_retry(response) and attempt < self.max_retries:
                delay = 2**attempt
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        delay = max(delay, int(retry_after))
                time.sleep(delay)
                continue

            _raise_for_error(response)
            return response

        if last_exc:
            raise last_exc
        raise RuntimeError("unreachable")

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if "params" in kwargs:
            kwargs["params"] = _prepare_params(kwargs["params"])
        return self._request_with_retry(method, path, **kwargs).json()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params or {})

    def download(
        self,
        path: str,
        dest: Path,
        params: dict[str, Any] | None = None,
        *,
        reporter: Any = None,
        label: str | None = None,
    ) -> Path:
        """Download binary content to a file. Returns the destination path.

        When a ``reporter`` is given the file is streamed and progress is
        reported via ``reporter.download_started`` / ``download_progress``.
        Without a reporter the body is loaded in memory (faster for small
        files).
        """
        if reporter is None:
            response = self._request_with_retry(
                "GET", path, params=_prepare_params(params) if params else {},
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(response.content)
            return dest

        prepared = _prepare_params(params) if params else {}
        last_exc: Exception | None = None
        for attempt in range(1 + self.max_retries):
            try:
                with self._client.stream("GET", path, params=prepared) as response:
                    if _should_retry(response) and attempt < self.max_retries:
                        delay = 2**attempt
                        if response.status_code == 429:
                            retry_after = response.headers.get("Retry-After")
                            if retry_after:
                                delay = max(delay, int(retry_after))
                        time.sleep(delay)
                        continue
                    _raise_for_error(response)
                    # Content-Length is the compressed size when the response
                    # is gzip/br encoded, but ``iter_bytes`` yields decompressed
                    # bytes — skip the total in that case so the bar stays
                    # indeterminate instead of showing X/Y where X>Y.
                    encoded = bool(response.headers.get("Content-Encoding"))
                    total_raw = response.headers.get("Content-Length")
                    total = int(total_raw) if total_raw and not encoded else None
                    reporter.download_started(label or path, total)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    n = 0
                    with dest.open("wb") as f:
                        for chunk in response.iter_bytes():
                            f.write(chunk)
                            n += len(chunk)
                            reporter.download_progress(n)
                    reporter.download_finished()
                    return dest
            except httpx.TimeoutException as exc:
                last_exc = TimeoutError(str(exc))
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise last_exc from exc
            except httpx.ConnectError as exc:
                last_exc = ConnectionError(str(exc))
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise last_exc from exc

        if last_exc:
            raise last_exc
        raise RuntimeError("unreachable")

    def close(self) -> None:
        self._client.close()


class AsyncHTTPClient:
    """Asynchronous HTTP transport with auth, retry, and error mapping."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.api_key = api_key or os.environ.get("MARKETLENS_API_KEY", "")
        self.base_url = (os.environ.get("MARKETLENS_BASE_URL") or base_url).rstrip("/")
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "User-Agent": _user_agent(),
                "Authorization": f"Bearer {self.api_key}",
            },
        )

    async def _request_with_retry(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute a request with retry logic. Returns the raw response."""
        last_exc: Exception | None = None
        for attempt in range(1 + self.max_retries):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                last_exc = TimeoutError(str(exc))
                if attempt < self.max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                raise last_exc from exc
            except httpx.ConnectError as exc:
                last_exc = ConnectionError(str(exc))
                if attempt < self.max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                raise last_exc from exc

            if _should_retry(response) and attempt < self.max_retries:
                delay = 2**attempt
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        delay = max(delay, int(retry_after))
                await asyncio.sleep(delay)
                continue

            _raise_for_error(response)
            return response

        if last_exc:
            raise last_exc
        raise RuntimeError("unreachable")

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if "params" in kwargs:
            kwargs["params"] = _prepare_params(kwargs["params"])
        return (await self._request_with_retry(method, path, **kwargs)).json()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params or {})

    async def download(
        self,
        path: str,
        dest: Path,
        params: dict[str, Any] | None = None,
        *,
        reporter: Any = None,
        label: str | None = None,
    ) -> Path:
        """Download binary content to a file. Returns the destination path."""
        if reporter is None:
            response = await self._request_with_retry(
                "GET", path, params=_prepare_params(params) if params else {},
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(response.content)
            return dest

        prepared = _prepare_params(params) if params else {}
        last_exc: Exception | None = None
        for attempt in range(1 + self.max_retries):
            try:
                async with self._client.stream("GET", path, params=prepared) as response:
                    if _should_retry(response) and attempt < self.max_retries:
                        delay = 2**attempt
                        if response.status_code == 429:
                            retry_after = response.headers.get("Retry-After")
                            if retry_after:
                                delay = max(delay, int(retry_after))
                        await asyncio.sleep(delay)
                        continue
                    _raise_for_error(response)
                    encoded = bool(response.headers.get("Content-Encoding"))
                    total_raw = response.headers.get("Content-Length")
                    total = int(total_raw) if total_raw and not encoded else None
                    reporter.download_started(label or path, total)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    n = 0
                    with dest.open("wb") as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
                            n += len(chunk)
                            reporter.download_progress(n)
                    reporter.download_finished()
                    return dest
            except httpx.TimeoutException as exc:
                last_exc = TimeoutError(str(exc))
                if attempt < self.max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                raise last_exc from exc
            except httpx.ConnectError as exc:
                last_exc = ConnectionError(str(exc))
                if attempt < self.max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                raise last_exc from exc

        if last_exc:
            raise last_exc
        raise RuntimeError("unreachable")

    async def close(self) -> None:
        await self._client.aclose()
