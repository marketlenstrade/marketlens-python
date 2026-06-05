from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from marketlens._base import AsyncHTTPClient, SyncHTTPClient
from marketlens._constants import DEFAULT_BASE_URL, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from marketlens.exceptions import NotFoundError
from marketlens.resources.events import AsyncEvents, Events
from marketlens.resources.exports import AsyncExports, Exports
from marketlens.resources.markets import AsyncMarkets, Markets
from marketlens.resources.orderbook import AsyncOrderbook, Orderbook
from marketlens.resources.reference import AsyncReference, Reference
from marketlens.resources.series import AsyncSeriesResource, SeriesResource
from marketlens.resources.signals import AsyncSignals, Signals


def _needs_download(data_dir: str) -> bool:
    """True when ``data_dir`` doesn't exist or has no history parquets yet."""
    path = Path(data_dir)
    if not path.exists():
        return True
    return not any(path.glob("history-*.parquet"))


class MarketLens:
    """Synchronous MarketLens API client.

    Args:
        api_key: API key. Falls back to ``MARKETLENS_API_KEY`` env var.
        base_url: API base URL.
        timeout: Request timeout. Pass a number for a uniform per-phase
            timeout, or an ``httpx.Timeout`` for granular connect/read/write/pool
            control. Long-running download endpoints override the read timeout
            internally so streaming responses aren't cut off.
        max_retries: Max retries on 429/5xx errors.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._http = SyncHTTPClient(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries,
        )
        self.markets = Markets(self._http)
        self.events = Events(self._http)
        self.series = SeriesResource(self._http)
        self.orderbook = Orderbook(self._http, series=self.series, markets=self.markets, events=self.events)
        self.signals = Signals(self._http)
        self.exports = Exports(self._http, markets=self.markets, series=self.series)
        self.reference = Reference(self._http)

    def backtest(
        self,
        strategy: Any,
        id: str | list[str],
        *,
        after: Any = None,
        before: Any = None,
        initial_cash: float | int | str,
        fees: str | None = "polymarket",
        include_trades: bool = True,
        latency_ms: int = 50,
        slippage_bps: int = 0,
        limit_fill_rate: float = 0.1,
        queue_position: bool = False,
        settlement_delay_ms: int = 5000,
        data_dir: str | None = None,
        progress: bool = True,
        coalesce: bool | None = None,
        **params: Any,
    ) -> Any:
        """Run a backtest on a market, series, or list of markets/series.

        Args:
            data_dir: Local Parquet directory for offline replay. Missing files
                are auto-downloaded on first run (creates the directory if
                absent); present files are reused. Files are named
                ``history-{market_id}.parquet`` (full) or
                ``history-{market_id}-compact.parquet`` (trade-aligned). The
                engine auto-picks the variant matching the strategy.
            progress: Show rich progress bars for fetching and backtesting.
                Auto-disables in non-TTY. Override via ``MARKETLENS_PROGRESS=0``.
            coalesce: Tri-state override for the compact data path.
                ``None`` (default) auto-detects from the strategy hooks.
                ``True`` forces compact (requires ``queue_position=False``
                and ``include_trades=True``). ``False`` forces full firehose.
                Fill prices are mode-independent — the override only
                controls inter-trade event density.

        Simple one-liner API. For advanced config, use ``BacktestEngine`` directly.
        """
        from marketlens.backtest import BacktestConfig, BacktestEngine

        config = BacktestConfig(
            initial_cash=initial_cash,
            fees=fees,
            include_trades=include_trades,
            latency_ms=latency_ms,
            slippage_bps=slippage_bps,
            limit_fill_rate=limit_fill_rate,
            queue_position=queue_position,
            settlement_delay_ms=settlement_delay_ms,
            progress=progress,
            coalesce=coalesce,
        )
        engine = BacktestEngine(strategy, config)
        # Auto-download (when ``data_dir`` is missing/empty) is dispatched
        # from inside engine.run after the market-resolution log, so the
        # status line and the "Downloading" bar appear in the right order.
        return engine.run(self, id, after=after, before=before, data_dir=data_dir, **params)

    def _ensure_exports_downloaded(
        self,
        id: str | list[str],
        data_dir: str,
        *,
        after: Any,
        before: Any,
        coalesce: bool,
        progress: bool,
    ) -> None:
        ids = id if isinstance(id, list) else [id]
        for one in ids:
            try:
                self.exports.download(
                    one, data_dir=data_dir, coalesce=coalesce, progress=progress,
                )
            except NotFoundError:
                self.exports.download_series(
                    one, after=after, before=before,
                    data_dir=data_dir, coalesce=coalesce, progress=progress,
                )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MarketLens:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class AsyncMarketLens:
    """Asynchronous MarketLens API client.

    Args:
        api_key: API key. Falls back to ``MARKETLENS_API_KEY`` env var.
        base_url: API base URL.
        timeout: Request timeout. Pass a number for a uniform per-phase
            timeout, or an ``httpx.Timeout`` for granular connect/read/write/pool
            control. Long-running download endpoints override the read timeout
            internally so streaming responses aren't cut off.
        max_retries: Max retries on 429/5xx errors.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._http = AsyncHTTPClient(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries,
        )
        self.markets = AsyncMarkets(self._http)
        self.events = AsyncEvents(self._http)
        self.series = AsyncSeriesResource(self._http)
        self.orderbook = AsyncOrderbook(self._http, series=self.series, markets=self.markets, events=self.events)
        self.signals = AsyncSignals(self._http)
        self.exports = AsyncExports(self._http, markets=self.markets, series=self.series)
        self.reference = AsyncReference(self._http)

    async def backtest(
        self,
        strategy: Any,
        id: str | list[str],
        *,
        after: Any = None,
        before: Any = None,
        initial_cash: float | int | str,
        fees: str | None = "polymarket",
        include_trades: bool = True,
        latency_ms: int = 50,
        slippage_bps: int = 0,
        limit_fill_rate: float = 0.1,
        queue_position: bool = False,
        settlement_delay_ms: int = 5000,
        data_dir: str | None = None,
        progress: bool = True,
        coalesce: bool | None = None,
        **params: Any,
    ) -> Any:
        """Run a backtest on a market, series, or list of markets/series (async).

        See :meth:`MarketLens.backtest` for ``data_dir`` and ``coalesce`` semantics.
        """
        from marketlens.backtest import AsyncBacktestEngine, BacktestConfig

        config = BacktestConfig(
            initial_cash=initial_cash,
            fees=fees,
            include_trades=include_trades,
            latency_ms=latency_ms,
            slippage_bps=slippage_bps,
            limit_fill_rate=limit_fill_rate,
            queue_position=queue_position,
            settlement_delay_ms=settlement_delay_ms,
            progress=progress,
            coalesce=coalesce,
        )
        engine = AsyncBacktestEngine(strategy, config)

        if data_dir is not None and _needs_download(data_dir):
            # Directory missing or empty: fetch the bulk export first.
            await self._ensure_exports_downloaded(
                id, data_dir,
                after=after, before=before,
                coalesce=engine._resolve_compact_mode(),
                progress=progress,
            )

        return await engine.run(self, id, after=after, before=before, data_dir=data_dir, **params)

    async def _ensure_exports_downloaded(
        self,
        id: str | list[str],
        data_dir: str,
        *,
        after: Any,
        before: Any,
        coalesce: bool,
        progress: bool,
    ) -> None:
        ids = id if isinstance(id, list) else [id]
        for one in ids:
            try:
                await self.exports.download(
                    one, data_dir=data_dir, coalesce=coalesce, progress=progress,
                )
            except NotFoundError:
                await self.exports.download_series(
                    one, after=after, before=before,
                    data_dir=data_dir, coalesce=coalesce, progress=progress,
                )

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> AsyncMarketLens:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
