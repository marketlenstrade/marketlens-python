from __future__ import annotations

from pathlib import Path
from typing import Any

from stream_unzip import async_stream_unzip, stream_unzip

from marketlens._base import AsyncHTTPClient, SyncHTTPClient, _coerce_timestamp
from marketlens._progress import make_reporter
from marketlens.exceptions import NotFoundError


def _extracted_name(raw: bytes | str) -> str:
    """Decode stream-unzip's bytes name to a string."""
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


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
        progress: bool = True,
        coalesce: bool = True,
    ) -> Path:
        """Download all data needed to backtest a single market.

        Downloads the market's order book history and, for crypto markets,
        tick-level reference trades for the underlying asset.

        Args:
            market_id: Market UUID.
            path: Directory to save files in.
            progress: Show a rich progress bar. Auto-disables in non-TTY.
            coalesce: When True (default), download the trade-aligned compact
                variant — ~4× smaller, book exact at every trade and snapshot.
                Set False for the full firehose when your strategy needs every
                inter-trade delta (e.g. ``queue_position=True``). The two
                variants are cached on disk separately and can coexist.

        Returns:
            Path to the data directory.
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        suffix = "-compact" if coalesce else ""
        params = {"coalesce": "true"} if coalesce else None

        with make_reporter(enabled=progress, n_markets=0) as reporter:
            dest = data_dir / f"history-{market_id}{suffix}.parquet"
            if not dest.exists():
                self._client.download(
                    f"/markets/{market_id}/export", dest,
                    params=params,
                    reporter=reporter, label=f"market {market_id[:8]}",
                )

            if self._markets is not None:
                try:
                    market = self._markets.get(market_id)
                    if market.underlying and market.open_time and market.close_time:
                        self._ensure_reference(
                            data_dir, market.underlying,
                            market.open_time, market.close_time,
                            reporter=reporter,
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
        progress: bool = True,
        coalesce: bool = True,
    ) -> Path:
        """Download all data needed to backtest a series.

        Downloads order book history for every market in the series and,
        for crypto series, tick-level reference trades for the underlying.

        Args:
            series_id: Series slug or UUID.
            after: Start time filter (ms epoch or datetime).
            before: End time filter (ms epoch or datetime).
            path: Directory to save files in.
            progress: Show a rich progress bar. Auto-disables in non-TTY.
            coalesce: See :meth:`download`. Default True.

        Returns:
            Path to the data directory.
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = _coerce_timestamp(after)
        if before is not None:
            params["before"] = _coerce_timestamp(before)
        if coalesce:
            params["coalesce"] = "true"

        with make_reporter(enabled=progress, n_markets=0) as reporter:
            chunks = self._client.stream_bytes(
                f"/series/{series_id}/export", params=params,
                reporter=reporter, label=f"series {series_id}",
            )
            # Stream-extract: each parquet lands at its final path as soon as
            # its bytes finish flowing. Already-extracted files are skipped so
            # a partial-then-resumed download picks up where it left off.
            # In-progress writes go through a ``.part`` file and are renamed
            # atomically — so a crash mid-member never leaves a half-written
            # final file.
            for raw_name, _size, member_chunks in stream_unzip(chunks):
                name = _extracted_name(raw_name)
                final = data_dir / name
                if final.exists():
                    # stream-unzip requires each member's chunks to be drained
                    # before advancing to the next member.
                    for _ in member_chunks:
                        pass
                    continue
                tmp = data_dir / (name + ".part")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with tmp.open("wb") as f:
                        for chunk in member_chunks:
                            f.write(chunk)
                    tmp.replace(final)
                except BaseException:
                    if tmp.exists():
                        tmp.unlink()
                    raise

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
                        self._ensure_reference(
                            data_dir, underlying, first_open, last_close,
                            reporter=reporter,
                        )
                except Exception:
                    pass

        return data_dir

    def _ensure_reference(
        self, data_dir: Path, symbol: str, after: int, before: int,
        *, reporter: Any = None,
    ) -> None:
        """Download reference trades if not already present."""
        dest = data_dir / f"reference-{symbol}.parquet"
        if dest.exists():
            return
        try:
            self._client.download(
                "/reference/trades/export", dest,
                params={
                    "symbol": symbol,
                    "after": _coerce_timestamp(after),
                    "before": _coerce_timestamp(before),
                },
                reporter=reporter, label=f"reference {symbol}",
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
        progress: bool = True,
        coalesce: bool = True,
    ) -> Path:
        """Download all data needed to backtest a single market.

        See :meth:`Exports.download` for argument semantics.
        """
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        suffix = "-compact" if coalesce else ""
        params = {"coalesce": "true"} if coalesce else None

        with make_reporter(enabled=progress, n_markets=0) as reporter:
            dest = data_dir / f"history-{market_id}{suffix}.parquet"
            if not dest.exists():
                await self._client.download(
                    f"/markets/{market_id}/export", dest,
                    params=params,
                    reporter=reporter, label=f"market {market_id[:8]}",
                )

            if self._markets is not None:
                try:
                    market = await self._markets.get(market_id)
                    if market.underlying and market.open_time and market.close_time:
                        await self._ensure_reference(
                            data_dir, market.underlying,
                            market.open_time, market.close_time,
                            reporter=reporter,
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
        progress: bool = True,
        coalesce: bool = True,
    ) -> Path:
        """Download all data needed to backtest a series."""
        data_dir = Path(path)
        data_dir.mkdir(parents=True, exist_ok=True)

        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = _coerce_timestamp(after)
        if before is not None:
            params["before"] = _coerce_timestamp(before)
        if coalesce:
            params["coalesce"] = "true"

        with make_reporter(enabled=progress, n_markets=0) as reporter:
            chunks = self._client.stream_bytes(
                f"/series/{series_id}/export", params=params,
                reporter=reporter, label=f"series {series_id}",
            )
            async for raw_name, _size, member_chunks in async_stream_unzip(chunks):
                name = _extracted_name(raw_name)
                final = data_dir / name
                if final.exists():
                    async for _ in member_chunks:
                        pass
                    continue
                tmp = data_dir / (name + ".part")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with tmp.open("wb") as f:
                        async for chunk in member_chunks:
                            f.write(chunk)
                    tmp.replace(final)
                except BaseException:
                    if tmp.exists():
                        tmp.unlink()
                    raise

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
                        await self._ensure_reference(
                            data_dir, underlying, first_open, last_close,
                            reporter=reporter,
                        )
                except Exception:
                    pass

        return data_dir

    async def _ensure_reference(
        self, data_dir: Path, symbol: str, after: int, before: int,
        *, reporter: Any = None,
    ) -> None:
        """Download reference trades if not already present."""
        dest = data_dir / f"reference-{symbol}.parquet"
        if dest.exists():
            return
        try:
            await self._client.download(
                "/reference/trades/export", dest,
                params={
                    "symbol": symbol,
                    "after": _coerce_timestamp(after),
                    "before": _coerce_timestamp(before),
                },
                reporter=reporter, label=f"reference {symbol}",
            )
        except NotFoundError:
            pass
