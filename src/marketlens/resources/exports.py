from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

from marketlens._base import AsyncHTTPClient, SyncHTTPClient
from marketlens.exceptions import NotFoundError


class Exports:
    def __init__(self, client: SyncHTTPClient, **_: Any) -> None:
        self._client = client

    def download(
        self,
        market_id: str,
        *,
        path: str | Path = ".",
    ) -> Path:
        """Download a market's full history (snapshots + deltas + trades) as Parquet.

        Args:
            market_id: Market UUID.
            path: Directory to save the file in.

        Returns:
            Path to the data directory (same as *path*).
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)
        dest = data_dir / f"history-{market_id}.parquet"
        if not dest.exists():
            self._client.download(f"/markets/{market_id}/export", dest)
        return data_dir

    def download_series(
        self,
        series_id: str,
        *,
        after: Any = None,
        before: Any = None,
        path: str | Path = ".",
    ) -> Path:
        """Download history for all markets in a series.

        Downloads a zip from the API containing one Parquet file per market,
        then extracts to the target directory. Skips if files already exist.

        Args:
            series_id: Series slug or UUID.
            after: Start time filter (ms epoch or datetime).
            before: End time filter (ms epoch or datetime).
            path: Directory to save files in.

        Returns:
            Path to the data directory (same as *path*).
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before

        response = self._client._request_with_retry(
            "GET", f"/series/{series_id}/export", params=params,
        )
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            for name in zf.namelist():
                dest = data_dir / name
                if not dest.exists():
                    dest.write_bytes(zf.read(name))

        return data_dir

    def download_reference(
        self,
        symbol: str,
        *,
        after: Any,
        before: Any,
        path: str | Path = ".",
    ) -> Path:
        """Download tick-level reference trades as Parquet.

        Args:
            symbol: Underlying symbol (e.g. BTC, ETH, SOL).
            after: Start time (ms epoch or datetime).
            before: End time (ms epoch or datetime).
            path: Directory to save the file in.

        Returns:
            Path to the data directory (same as *path*).
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)
        dest = data_dir / f"reference-{symbol}.parquet"
        if not dest.exists():
            self._client.download(
                "/reference/trades/export",
                dest,
                params={"symbol": symbol, "after": after, "before": before},
            )
        return data_dir


class AsyncExports:
    def __init__(self, client: AsyncHTTPClient, **_: Any) -> None:
        self._client = client

    async def download(
        self,
        market_id: str,
        *,
        path: str | Path = ".",
    ) -> Path:
        """Download a market's full history (snapshots + deltas + trades) as Parquet.

        Args:
            market_id: Market UUID.
            path: Directory to save the file in.

        Returns:
            Path to the data directory (same as *path*).
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)
        dest = data_dir / f"history-{market_id}.parquet"
        if not dest.exists():
            await self._client.download(f"/markets/{market_id}/export", dest)
        return data_dir

    async def download_series(
        self,
        series_id: str,
        *,
        after: Any = None,
        before: Any = None,
        path: str | Path = ".",
    ) -> Path:
        """Download history for all markets in a series (async).

        Args:
            series_id: Series slug or UUID.
            after: Start time filter (ms epoch or datetime).
            before: End time filter (ms epoch or datetime).
            path: Directory to save files in.

        Returns:
            Path to the data directory (same as *path*).
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before

        response = await self._client._request_with_retry(
            "GET", f"/series/{series_id}/export", params=params,
        )
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            for name in zf.namelist():
                dest = data_dir / name
                if not dest.exists():
                    dest.write_bytes(zf.read(name))

        return data_dir

    async def download_reference(
        self,
        symbol: str,
        *,
        after: Any,
        before: Any,
        path: str | Path = ".",
    ) -> Path:
        """Download tick-level reference trades as Parquet (async).

        Args:
            symbol: Underlying symbol (e.g. BTC, ETH, SOL).
            after: Start time (ms epoch or datetime).
            before: End time (ms epoch or datetime).
            path: Directory to save the file in.

        Returns:
            Path to the data directory (same as *path*).
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)
        dest = data_dir / f"reference-{symbol}.parquet"
        if not dest.exists():
            await self._client.download(
                "/reference/trades/export",
                dest,
                params={"symbol": symbol, "after": after, "before": before},
            )
        return data_dir
