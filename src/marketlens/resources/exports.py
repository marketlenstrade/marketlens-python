from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from marketlens._base import AsyncHTTPClient, SyncHTTPClient, _coerce_timestamp
from marketlens._progress import make_reporter
from marketlens.exceptions import NotFoundError

# Reference trades are fetched a touch before the first market opens so a price
# at/before the open is always available. Without it, a market opening on the
# exact window boundary (e.g. midnight) has no prior tick — the underlying's
# first trade lands a few hundred ms later — and ``reference_price()`` returns
# None there. 60s is ample for liquid crypto underlyings and costs a handful of
# extra ticks; the price lookup still returns the closest tick <= the query.
_REFERENCE_LOOKBACK_MS = 60_000


@dataclass(frozen=True)
class SeriesPending:
    market_id: str
    status: str


@dataclass(frozen=True)
class SeriesFailed:
    market_id: str
    error: str


@dataclass(frozen=True)
class SeriesRateLimited:
    market_id: str
    events: int


@dataclass(frozen=True)
class SeriesDownloadResult:
    """Outcome of ``client.exports.download_series``.

    Implements ``os.PathLike`` so callers can pass the result directly anywhere
    a directory is expected (e.g. ``client.backtest(..., data_dir=result)``).
    """
    data_dir: Path
    ready: list[str] = field(default_factory=list)
    pending: list[SeriesPending] = field(default_factory=list)
    failed: list[SeriesFailed] = field(default_factory=list)
    rate_limited: list[SeriesRateLimited] = field(default_factory=list)
    events_charged: int = 0

    def __fspath__(self) -> str:
        return str(self.data_dir)


class Exports:
    def __init__(self, client: SyncHTTPClient, *, markets: Any = None, series: Any = None) -> None:
        self._client = client
        self._markets = markets
        self._series = series

    def download(
        self,
        market_id: str,
        *,
        data_dir: str | Path = ".",
        progress: bool = True,
        coalesce: bool = True,
    ) -> Path:
        """Download all data needed to backtest a single market.

        Downloads the market's order book history and, for crypto markets,
        tick-level reference trades for the underlying asset.

        Args:
            market_id: Market UUID.
            data_dir: Directory to save files in. Created if missing. Pass
                the same directory to ``client.backtest(data_dir=...)`` to
                replay against it.
            progress: Show a rich progress bar. Auto-disables in non-TTY.
            coalesce: When True (default), download the trade-aligned compact
                variant, ~4x smaller, book exact at every trade and snapshot.
                Set False for the full firehose when your strategy needs every
                inter-trade delta (e.g. ``queue_position=True``). The two
                variants are cached on disk separately and can coexist.

        Returns:
            Path to the data directory.

        Raises:
            ExportNotReadyError: The pre-built parquet for this market is not
                on the bucket yet. Try again later or pick a different market.
        """
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        suffix = "-compact" if coalesce else ""
        params = {"coalesce": "true"} if coalesce else None

        with make_reporter(enabled=progress, n_markets=0) as reporter:
            dest = data_dir / f"history-{market_id}{suffix}.parquet"
            if not dest.exists():
                self._client.download_via_redirect(
                    f"/markets/{market_id}/export", dest,
                    params=params,
                    reporter=reporter, label=market_id,
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
        data_dir: str | Path = ".",
        progress: bool = True,
        coalesce: bool = True,
        concurrency: int = 1,
    ) -> SeriesDownloadResult:
        """Download all data needed to backtest a series.

        The server returns a JSON manifest partitioning markets by state. Ready
        markets have a presigned URL we fetch; ``pending`` and ``failed`` are
        surfaced on the result for caller inspection.

        Args:
            series_id: Series slug or UUID.
            after: Start time filter (ms epoch or datetime).
            before: End time filter (ms epoch or datetime).
            data_dir: Directory to save files in. Created if missing. Pass
                the same directory to ``client.backtest(data_dir=...)`` to
                replay against it.
            progress: Show a rich progress bar. Auto-disables in non-TTY.
            coalesce: See :meth:`download`. Default True.
            concurrency: Number of concurrent per-market downloads. Default 1.

        Returns:
            ``SeriesDownloadResult`` with ``data_dir``, ``ready``, ``pending``,
            ``failed``, ``rate_limited``, and ``events_charged``.
            ``rate_limited`` lists markets that were skipped because including
            them would have exceeded the caller's daily event budget; retry
            after the budget resets or with a narrower ``after``/``before``
            window. The result is ``os.PathLike`` (its ``__fspath__`` returns
            the data directory), so it can be passed directly to
            ``client.backtest(..., data_dir=result)``.
        """
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = _coerce_timestamp(after)
        if before is not None:
            params["before"] = _coerce_timestamp(before)
        if coalesce:
            params["coalesce"] = "true"

        body = self._client.get(f"/series/{series_id}/export", params=params)
        suffix = "-compact" if coalesce else ""
        pending = [SeriesPending(e["market_id"], e["status"]) for e in body.get("pending", [])]
        failed = [SeriesFailed(e["market_id"], e["error"]) for e in body.get("failed", [])]
        rate_limited = [
            SeriesRateLimited(e["market_id"], int(e.get("events", 0)))
            for e in body.get("rate_limited", [])
        ]
        events_charged = int(body.get("events_charged", 0))
        targets = [(e["market_id"], e["url"]) for e in body.get("ready", [])]

        def _one(market_id: str, url: str, reporter: Any) -> str:
            dest = data_dir / f"history-{market_id}{suffix}.parquet"
            if not dest.exists():
                self._client.fetch_presigned(
                    url, dest,
                    reporter=reporter, label=f"market {market_id[:8]}",
                )
            reporter.batch_download_advance()
            return market_id

        with make_reporter(enabled=progress, n_markets=len(targets)) as reporter:
            if targets:
                reporter.batch_download_started(f"Downloading {series_id}", len(targets))
            if concurrency <= 1 or len(targets) <= 1:
                ready = [_one(m, u, reporter) for m, u in targets]
            else:
                with ThreadPoolExecutor(max_workers=concurrency) as ex:
                    futures = [ex.submit(_one, m, u, reporter) for m, u in targets]
                    ready = [f.result() for f in futures]

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

        return SeriesDownloadResult(
            data_dir=data_dir,
            ready=ready,
            pending=pending,
            failed=failed,
            rate_limited=rate_limited,
            events_charged=events_charged,
        )

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
                    "after": _coerce_timestamp(after) - _REFERENCE_LOOKBACK_MS,
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
        data_dir: str | Path = ".",
        progress: bool = True,
        coalesce: bool = True,
    ) -> Path:
        """Download all data needed to backtest a single market.

        See :meth:`Exports.download` for argument semantics.
        """
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        suffix = "-compact" if coalesce else ""
        params = {"coalesce": "true"} if coalesce else None

        with make_reporter(enabled=progress, n_markets=0) as reporter:
            dest = data_dir / f"history-{market_id}{suffix}.parquet"
            if not dest.exists():
                await self._client.download_via_redirect(
                    f"/markets/{market_id}/export", dest,
                    params=params,
                    reporter=reporter, label=market_id,
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
        data_dir: str | Path = ".",
        progress: bool = True,
        coalesce: bool = True,
        concurrency: int = 1,
    ) -> SeriesDownloadResult:
        """Async equivalent of :meth:`Exports.download_series`."""
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = _coerce_timestamp(after)
        if before is not None:
            params["before"] = _coerce_timestamp(before)
        if coalesce:
            params["coalesce"] = "true"

        body = await self._client.get(f"/series/{series_id}/export", params=params)
        suffix = "-compact" if coalesce else ""
        pending = [SeriesPending(e["market_id"], e["status"]) for e in body.get("pending", [])]
        failed = [SeriesFailed(e["market_id"], e["error"]) for e in body.get("failed", [])]
        rate_limited = [
            SeriesRateLimited(e["market_id"], int(e.get("events", 0)))
            for e in body.get("rate_limited", [])
        ]
        events_charged = int(body.get("events_charged", 0))
        targets = [(e["market_id"], e["url"]) for e in body.get("ready", [])]

        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(market_id: str, url: str, reporter: Any) -> str:
            async with sem:
                dest = data_dir / f"history-{market_id}{suffix}.parquet"
                if not dest.exists():
                    await self._client.fetch_presigned(
                        url, dest,
                        reporter=reporter, label=f"market {market_id[:8]}",
                    )
                reporter.batch_download_advance()
                return market_id

        with make_reporter(enabled=progress, n_markets=len(targets)) as reporter:
            if targets:
                reporter.batch_download_started(f"Downloading {series_id}", len(targets))
                ready = list(await asyncio.gather(*[_one(m, u, reporter) for m, u in targets]))
            else:
                ready = []

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

        return SeriesDownloadResult(
            data_dir=data_dir,
            ready=ready,
            pending=pending,
            failed=failed,
            rate_limited=rate_limited,
            events_charged=events_charged,
        )

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
                    "after": _coerce_timestamp(after) - _REFERENCE_LOOKBACK_MS,
                    "before": _coerce_timestamp(before),
                },
                reporter=reporter, label=f"reference {symbol}",
            )
        except NotFoundError:
            pass
