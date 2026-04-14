from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

from marketlens._base import AsyncHTTPClient, SyncHTTPClient
from marketlens.exceptions import NotFoundError


class Exports:
    def __init__(self, client: SyncHTTPClient, *, markets: Any = None, series: Any = None) -> None:
        self._client = client
        self._markets = markets
        self._series = series

    def download(
        self,
        market_id: str,
        *,
        path: str | Path = ".",
    ) -> Path:
        """Download all data needed to backtest a single market.

        Downloads the market's order book history and, for crypto markets,
        tick-level reference trades for the underlying asset.

        Args:
            market_id: Market UUID.
            path: Directory to save files in.

        Returns:
            Path to the data directory.
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        # Market history
        dest = data_dir / f"history-{market_id}.parquet"
        if not dest.exists():
            self._client.download(f"/markets/{market_id}/export", dest)

        # Reference trades for the underlying
        if self._markets is not None:
            try:
                market = self._markets.get(market_id)
                if market.underlying and market.open_time and market.close_time:
                    self._ensure_reference(
                        data_dir, market.underlying,
                        market.open_time, market.close_time,
                    )
            except Exception:
                pass

        return data_dir

    def download_series(
        self,
        series_id: str,
        *,
        after: Any = None,
        before: Any = None,
        path: str | Path = ".",
    ) -> Path:
        """Download all data needed to backtest a series.

        Downloads order book history for every market in the series and,
        for crypto series, tick-level reference trades for the underlying.

        Args:
            series_id: Series slug or UUID.
            after: Start time filter (ms epoch or datetime).
            before: End time filter (ms epoch or datetime).
            path: Directory to save files in.

        Returns:
            Path to the data directory.
        """
        from marketlens._base import _coerce_timestamp

        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = _coerce_timestamp(after)
        if before is not None:
            params["before"] = _coerce_timestamp(before)

        # Download market histories as zip
        response = self._client._request_with_retry(
            "GET", f"/series/{series_id}/export", params=params,
        )
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            for name in zf.namelist():
                dest = data_dir / name
                if not dest.exists():
                    dest.write_bytes(zf.read(name))

        # Discover underlying from the first market in the series
        if self._series is not None:
            try:
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
                if underlying and first_open and last_close:
                    self._ensure_reference(data_dir, underlying, first_open, last_close)
            except Exception:
                pass

        return data_dir

    def _ensure_reference(
        self, data_dir: Path, symbol: str, after: int, before: int,
    ) -> None:
        """Download reference trades if not already present."""
        dest = data_dir / f"reference-{symbol}.parquet"
        if dest.exists():
            return
        try:
            self._client.download(
                "/reference/trades/export", dest,
                params={"symbol": symbol, "after": after, "before": before},
            )
        except NotFoundError:
            pass


class AsyncExports:
    def __init__(self, client: AsyncHTTPClient, *, markets: Any = None, series: Any = None) -> None:
        self._client = client
        self._markets = markets
        self._series = series

    async def download(
        self,
        market_id: str,
        *,
        path: str | Path = ".",
    ) -> Path:
        """Download all data needed to backtest a single market.

        Downloads the market's order book history and, for crypto markets,
        tick-level reference trades for the underlying asset.

        Args:
            market_id: Market UUID.
            path: Directory to save files in.

        Returns:
            Path to the data directory.
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        dest = data_dir / f"history-{market_id}.parquet"
        if not dest.exists():
            await self._client.download(f"/markets/{market_id}/export", dest)

        if self._markets is not None:
            try:
                market = await self._markets.get(market_id)
                if market.underlying and market.open_time and market.close_time:
                    await self._ensure_reference(
                        data_dir, market.underlying,
                        market.open_time, market.close_time,
                    )
            except Exception:
                pass

        return data_dir

    async def download_series(
        self,
        series_id: str,
        *,
        after: Any = None,
        before: Any = None,
        path: str | Path = ".",
    ) -> Path:
        """Download all data needed to backtest a series.

        Downloads order book history for every market in the series and,
        for crypto series, tick-level reference trades for the underlying.

        Args:
            series_id: Series slug or UUID.
            after: Start time filter (ms epoch or datetime).
            before: End time filter (ms epoch or datetime).
            path: Directory to save files in.

        Returns:
            Path to the data directory.
        """
        from marketlens._base import _coerce_timestamp

        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = _coerce_timestamp(after)
        if before is not None:
            params["before"] = _coerce_timestamp(before)

        response = await self._client._request_with_retry(
            "GET", f"/series/{series_id}/export", params=params,
        )
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            for name in zf.namelist():
                dest = data_dir / name
                if not dest.exists():
                    dest.write_bytes(zf.read(name))

        if self._series is not None:
            try:
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
                if underlying and first_open and last_close:
                    await self._ensure_reference(data_dir, underlying, first_open, last_close)
            except Exception:
                pass

        return data_dir

    async def _ensure_reference(
        self, data_dir: Path, symbol: str, after: int, before: int,
    ) -> None:
        """Download reference trades if not already present."""
        dest = data_dir / f"reference-{symbol}.parquet"
        if dest.exists():
            return
        try:
            await self._client.download(
                "/reference/trades/export", dest,
                params={"symbol": symbol, "after": after, "before": before},
            )
        except NotFoundError:
            pass
