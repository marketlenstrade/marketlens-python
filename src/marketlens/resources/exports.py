from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from marketlens._base import AsyncHTTPClient, SyncHTTPClient, _coerce_timestamp
from marketlens._progress import make_reporter
from marketlens.exceptions import ExportNotReadyError, IncompleteExportError, NotFoundError

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
    # Rows the row meter would charge to unlock this market (0 when the file
    # is already unlocked). Falls back to ``events`` against older servers.
    rows: int = 0


@dataclass(frozen=True)
class SeriesCoverage:
    """The series' data span, in the same terms as every other coverage
    block: ``data_start`` / ``data_end`` span all its markets' data
    (``data_end`` is None while any market is still open on the platform),
    ``collection_tier`` is "streamed", "polled", or "mixed". None against
    servers that predate the field."""
    data_start: int | None
    data_end: int | None
    collection_tier: str | None


def _parse_coverage(raw: Any) -> SeriesCoverage | None:
    if not isinstance(raw, dict):
        return None
    return SeriesCoverage(
        data_start=raw.get("data_start"), data_end=raw.get("data_end"),
        collection_tier=raw.get("collection_tier"),
    )


@dataclass(frozen=True)
class SeriesDownloadResult:
    """Outcome of ``client.exports.download_series``.

    Implements ``os.PathLike`` so callers can pass the result directly anywhere
    a directory is expected (e.g. ``client.backtest(..., data_dir=result)``).

    ``rows_charged`` is what the data-row meter charged: files this account
    already unlocked charge zero rows on repeat downloads. ``events_charged``
    is the legacy alias (full rows of every ready file) and is kept for one
    minor version.

    For a ``dry_run=True`` call, ``rows_charged`` is the exact cost a real
    call would bill right now and ``ready`` names the markets it would
    download; no files were written, nothing was billed or unlocked.
    """
    data_dir: Path
    ready: list[str] = field(default_factory=list)
    pending: list[SeriesPending] = field(default_factory=list)
    failed: list[SeriesFailed] = field(default_factory=list)
    rate_limited: list[SeriesRateLimited] = field(default_factory=list)
    events_charged: int = 0
    rows_charged: int = 0
    # The series' data span. None against older servers.
    coverage: SeriesCoverage | None = None
    # Why the export stopped short. None when nothing was withheld or
    # against older servers.
    wall: SeriesWall | None = None

    def __fspath__(self) -> str:
        return str(self.data_dir)


@dataclass(frozen=True)
class SeriesWall:
    """Why a series export stopped short, stated once by the server."""
    reason: str
    markets_withheld: int
    rows_needed: int
    resets_at: int | None
    upgrade_url: str | None


def _parse_wall(raw: Any) -> SeriesWall | None:
    if not raw:
        return None
    return SeriesWall(
        reason=str(raw.get("reason", "")),
        markets_withheld=int(raw.get("markets_withheld", 0)),
        rows_needed=int(raw.get("rows_needed", 0)),
        resets_at=raw.get("resets_at"),
        upgrade_url=raw.get("upgrade_url"),
    )


# Written into data_dir when the export stopped short, one withheld market id
# per line, so a partial directory is recognisable on disk.
INCOMPLETE_MARKER = ".incomplete"


def _finish_series(series_id: str, data_dir: Path, result: "SeriesDownloadResult") -> "SeriesDownloadResult":
    """Return the result, or raise ``IncompleteExportError`` once the ready
    files are on disk when the server withheld markets."""
    marker = data_dir / INCOMPLETE_MARKER
    if not result.rate_limited:
        try:
            marker.unlink()
        except OSError:
            pass
        return result
    missing = [e.market_id for e in result.rate_limited]
    wall = result.wall
    rows_needed = wall.rows_needed if wall else sum(e.rows or e.events for e in result.rate_limited)
    try:
        marker.write_text("\n".join(missing) + "\n")
    except OSError:
        pass
    raise IncompleteExportError(
        f"Series '{series_id}': {len(missing)} markets in the window were not"
        f" delivered because unlocking them needs {rows_needed:,} data rows"
        " and the account's remaining allowance does not cover it. Files"
        f" already downloaded are in {data_dir} and re-download free. Narrow"
        " the window, wait for the allowance reset, or upgrade the plan"
        + (f" at {wall.upgrade_url}" if wall and wall.upgrade_url else "") + ".",
        missing=missing,
        rows_needed=rows_needed,
        upgrade_url=wall.upgrade_url if wall else None,
        resets_at=wall.resets_at if wall else None,
        result=result,
    )


@dataclass(frozen=True)
class BarsDownloadResult:
    """Outcome of ``client.exports.download_market_bars_batch``.

    The bar-cadence parallel to :class:`SeriesDownloadResult`. ``pending`` lists
    markets whose export is still building (re-run to pick them up once built);
    ``not_found`` lists markets with no data. PathLike, so it can be passed
    straight to ``client.backtest(..., data_dir=result)``.
    """
    data_dir: Path
    ready: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)

    def __fspath__(self) -> str:
        return str(self.data_dir)


def _run_concurrent(items: list, work: Any, *, concurrency: int) -> list:
    """Run ``work(item)`` over ``items`` (concurrently when ``concurrency > 1``)
    and return the results in input order. Shared by the series and bar batch
    downloads so the fan-out lives in one place."""
    if concurrency <= 1 or len(items) <= 1:
        return [work(it) for it in items]
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        return [f.result() for f in [ex.submit(work, it) for it in items]]


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
        dry_run: bool = False,
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
            dry_run: When True, fetch the manifest only: nothing is
                downloaded, billed, or unlocked, and ``rows_charged`` on the
                result is the exact cost an identical call without
                ``dry_run`` would bill right now (files this account already
                unlocked count as zero). Use it to check the price of a
                window before spending rows on it.

        Returns:
            ``SeriesDownloadResult`` with ``data_dir``, ``ready``, ``pending``,
            ``failed``, ``rate_limited``, ``rows_charged``, and the legacy
            ``events_charged`` alias.
            When the server withheld markets because unlocking them would
            exceed the remaining row balance, the ready files are downloaded
            and then ``IncompleteExportError`` is raised, carrying the
            server's ``wall`` (rows needed, reset time, upgrade link) and
            the partial result; ``dry_run`` never raises. The result is
            ``os.PathLike`` (its ``__fspath__`` returns the data directory),
            so it can be passed directly to
            ``client.backtest(..., data_dir=result)``. With ``dry_run=True``
            the ``ready`` list names the markets a real call would download,
            but no files exist yet.
        """
        data_dir = Path(data_dir)
        if not dry_run:
            data_dir.mkdir(parents=True, exist_ok=True)

        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = _coerce_timestamp(after)
        if before is not None:
            params["before"] = _coerce_timestamp(before)
        if coalesce:
            params["coalesce"] = "true"
        if dry_run:
            params["dry_run"] = "true"

        body = self._client.get(f"/series/{series_id}/export", params=params)
        suffix = "-compact" if coalesce else ""
        pending = [SeriesPending(e["market_id"], e["status"]) for e in body.get("pending", [])]
        failed = [SeriesFailed(e["market_id"], e["error"]) for e in body.get("failed", [])]
        rate_limited = [
            SeriesRateLimited(
                e["market_id"],
                int(e.get("events", 0)),
                rows=int(e.get("rows", e.get("events", 0))),
            )
            for e in body.get("rate_limited", [])
        ]
        events_charged = int(body.get("events_charged", 0))
        rows_charged = int(body.get("rows_charged", events_charged))
        coverage = _parse_coverage(body.get("coverage"))
        wall = _parse_wall(body.get("wall"))

        if dry_run:
            return SeriesDownloadResult(
                data_dir=data_dir,
                ready=[e["market_id"] for e in body.get("ready", [])],
                pending=pending,
                failed=failed,
                rate_limited=rate_limited,
                events_charged=events_charged,
                rows_charged=rows_charged,
                coverage=coverage,
                wall=wall,
            )

        targets = [(e["market_id"], e["url"]) for e in body.get("ready", [])]

        with make_reporter(enabled=progress, n_markets=len(targets)) as reporter:
            if targets:
                reporter.batch_download_started(f"Downloading {series_id}", len(targets))

            def _one(target: tuple[str, str]) -> str:
                market_id, url = target
                dest = data_dir / f"history-{market_id}{suffix}.parquet"
                if not dest.exists():
                    self._client.fetch_presigned(
                        url, dest,
                        reporter=reporter, label=f"market {market_id[:8]}",
                    )
                reporter.batch_download_advance()
                return market_id

            ready = _run_concurrent(targets, _one, concurrency=concurrency)

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

        return _finish_series(series_id, data_dir, SeriesDownloadResult(
            data_dir=data_dir,
            ready=ready,
            pending=pending,
            failed=failed,
            rate_limited=rate_limited,
            events_charged=events_charged,
            rows_charged=rows_charged,
            coverage=coverage,
            wall=wall,
        ))

    def download_market_bars(
        self, market_id: str, *, resolution: str, price: str, data_dir: str | Path,
    ) -> Path:
        """Download the pre-built bar parquet for one market+resolution.

        The signal-level (alpha) backtest's offline source, the bar-cadence
        parallel to :meth:`download`. Keyed by market and resolution (the whole
        market, no time window), fetched the same way as the history export: a
        302 from the API to a presigned object-store URL. ``price="mid"`` pulls
        the metrics export, ``price="close"`` the candles export. The first
        request marks the variant for export and raises ``ExportNotReadyError``
        until the static-exporter builds it.
        """
        from marketlens.backtest._bar import bar_file

        dest = bar_file(data_dir, market_id, resolution, price)
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        endpoint = (
            f"/markets/{market_id}/orderbook/metrics/export" if price == "mid"
            else f"/markets/{market_id}/candles/export"
        )
        self._client.download_via_redirect(endpoint, dest, params={"resolution": resolution})
        return dest

    def download_market_bars_batch(
        self,
        market_ids: list[str],
        *,
        resolution: str,
        price: str,
        data_dir: str | Path,
        concurrency: int = 1,
        progress: bool = True,
    ) -> BarsDownloadResult:
        """Download many markets' bar exports concurrently, with a progress bar.

        The bar-cadence parallel to :meth:`download_series`: each market's
        ``price="mid"`` (metrics) or ``price="close"`` (candles) export at
        ``resolution``, fetched through :meth:`download_market_bars`. Cached files
        are reused. Variants still building land in ``pending`` (re-run to pick
        them up once built); markets with no data land in ``not_found``.
        """
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        targets = list(market_ids)

        with make_reporter(enabled=progress, n_markets=len(targets)) as reporter:
            if targets:
                reporter.batch_download_started("Downloading", len(targets))

            def _one(market_id: str) -> tuple[str, str]:
                try:
                    self.download_market_bars(
                        market_id, resolution=resolution, price=price, data_dir=data_dir,
                    )
                    state = "ready"
                except ExportNotReadyError:
                    state = "pending"
                except NotFoundError:
                    state = "not_found"
                except Exception:
                    state = "not_found"
                reporter.batch_download_advance()
                return market_id, state

            results = _run_concurrent(targets, _one, concurrency=concurrency)

        return BarsDownloadResult(
            data_dir=data_dir,
            ready=[m for m, s in results if s == "ready"],
            pending=[m for m, s in results if s == "pending"],
            not_found=[m for m, s in results if s == "not_found"],
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
        dry_run: bool = False,
    ) -> SeriesDownloadResult:
        """Async equivalent of :meth:`Exports.download_series`."""
        data_dir = Path(data_dir)
        if not dry_run:
            data_dir.mkdir(parents=True, exist_ok=True)

        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = _coerce_timestamp(after)
        if before is not None:
            params["before"] = _coerce_timestamp(before)
        if coalesce:
            params["coalesce"] = "true"
        if dry_run:
            params["dry_run"] = "true"

        body = await self._client.get(f"/series/{series_id}/export", params=params)
        suffix = "-compact" if coalesce else ""
        pending = [SeriesPending(e["market_id"], e["status"]) for e in body.get("pending", [])]
        failed = [SeriesFailed(e["market_id"], e["error"]) for e in body.get("failed", [])]
        rate_limited = [
            SeriesRateLimited(
                e["market_id"],
                int(e.get("events", 0)),
                rows=int(e.get("rows", e.get("events", 0))),
            )
            for e in body.get("rate_limited", [])
        ]
        events_charged = int(body.get("events_charged", 0))
        rows_charged = int(body.get("rows_charged", events_charged))
        coverage = _parse_coverage(body.get("coverage"))
        wall = _parse_wall(body.get("wall"))

        if dry_run:
            return SeriesDownloadResult(
                data_dir=data_dir,
                ready=[e["market_id"] for e in body.get("ready", [])],
                pending=pending,
                failed=failed,
                rate_limited=rate_limited,
                events_charged=events_charged,
                rows_charged=rows_charged,
                coverage=coverage,
                wall=wall,
            )

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

        return _finish_series(series_id, data_dir, SeriesDownloadResult(
            data_dir=data_dir,
            ready=ready,
            pending=pending,
            failed=failed,
            rate_limited=rate_limited,
            events_charged=events_charged,
            rows_charged=rows_charged,
            coverage=coverage,
            wall=wall,
        ))

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
