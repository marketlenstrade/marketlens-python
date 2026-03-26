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
        and downloads each market's history Parquet file. Also downloads
        tick-level reference trades for the underlying asset (if available).

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

        underlying = None
        first_open = None
        last_close = None

        for market in self._series.walk(series_id, after=after, before=before):
            if underlying is None and market.underlying:
                underlying = market.underlying
            if market.open_time is not None:
                if first_open is None or market.open_time < first_open:
                    first_open = market.open_time
            if market.close_time is not None:
                if last_close is None or market.close_time > last_close:
                    last_close = market.close_time
            try:
                self.download(market.id, path=data_dir)
            except NotFoundError:
                pass

        # Download tick-level reference trades for the underlying asset
        if underlying and first_open and last_close:
            ref_path = data_dir / f"reference-{underlying}.parquet"
            if not ref_path.exists():
                try:
                    self._client.download(
                        "/reference/trades/export",
                        ref_path,
                        params={"symbol": underlying, "after": first_open, "before": last_close},
                    )
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

        underlying = None
        first_open = None
        last_close = None

        async for market in self._series.walk(series_id, after=after, before=before):
            if underlying is None and market.underlying:
                underlying = market.underlying
            if market.open_time is not None:
                if first_open is None or market.open_time < first_open:
                    first_open = market.open_time
            if market.close_time is not None:
                if last_close is None or market.close_time > last_close:
                    last_close = market.close_time
            try:
                await self.download(market.id, path=data_dir)
            except NotFoundError:
                pass

        if underlying and first_open and last_close:
            ref_path = data_dir / f"reference-{underlying}.parquet"
            if not ref_path.exists():
                try:
                    await self._client.download(
                        "/reference/trades/export",
                        ref_path,
                        params={"symbol": underlying, "after": first_open, "before": last_close},
                    )
                except NotFoundError:
                    pass

        return data_dir
