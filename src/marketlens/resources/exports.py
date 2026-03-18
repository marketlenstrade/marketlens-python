from __future__ import annotations

from pathlib import Path
from typing import Any

from marketlens._base import AsyncHTTPClient, SyncHTTPClient
from marketlens.exceptions import NotFoundError


class Exports:
    def __init__(self, client: SyncHTTPClient, *, series: Any = None) -> None:
        self._client = client
        self._series = series

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
            Path to the downloaded Parquet file.
        """
        dest = Path(path) / f"history-{market_id}.parquet"
        if dest.exists():
            return dest
        return self._client.download(
            f"/markets/{market_id}/export",
            dest,
        )

    def download_series(
        self,
        series_id: str,
        *,
        after: Any = None,
        before: Any = None,
        path: str | Path = ".",
    ) -> Path:
        """Download history files for all markets in a series.

        Resolves the series, walks its markets filtered by after/before,
        and downloads each market's history Parquet file. Skips markets
        whose files already exist on disk.

        Args:
            series_id: Series slug or UUID.
            after: Only markets with close_time >= after.
            before: Only markets with open_time <= before.
            path: Directory to save files in.

        Returns:
            Path to the data directory (same as *path*).
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        for market in self._series.walk(series_id, after=after, before=before):
            try:
                self.download(market.id, path=data_dir)
            except NotFoundError:
                pass

        return data_dir


class AsyncExports:
    def __init__(self, client: AsyncHTTPClient, *, series: Any = None) -> None:
        self._client = client
        self._series = series

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
            Path to the downloaded Parquet file.
        """
        dest = Path(path) / f"history-{market_id}.parquet"
        if dest.exists():
            return dest
        return await self._client.download(
            f"/markets/{market_id}/export",
            dest,
        )

    async def download_series(
        self,
        series_id: str,
        *,
        after: Any = None,
        before: Any = None,
        path: str | Path = ".",
    ) -> Path:
        """Download history files for all markets in a series (async).

        Args:
            series_id: Series slug or UUID.
            after: Only markets with close_time >= after.
            before: Only markets with open_time <= before.
            path: Directory to save files in.

        Returns:
            Path to the data directory (same as *path*).
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        async for market in self._series.walk(series_id, after=after, before=before):
            try:
                await self.download(market.id, path=data_dir)
            except NotFoundError:
                pass

        return data_dir
