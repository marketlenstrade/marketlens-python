from __future__ import annotations

from typing import Any

from marketlens._base import AsyncHTTPClient, SyncHTTPClient
from marketlens._constants import DEFAULT_BASE_URL, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from marketlens.resources.events import AsyncEvents, Events
from marketlens.resources.exports import AsyncExports, Exports
from marketlens.resources.markets import AsyncMarkets, Markets
from marketlens.resources.orderbook import AsyncOrderbook, Orderbook
from marketlens.resources.reference import AsyncReference, Reference
from marketlens.resources.series import AsyncSeriesResource, SeriesResource
from marketlens.resources.signals import AsyncSignals, Signals


class MarketLens:
    """Synchronous MarketLens API client.

    Args:
        api_key: API key. Falls back to ``MARKETLENS_API_KEY`` env var.
        base_url: API base URL.
        timeout: Request timeout in seconds.
        max_retries: Max retries on 429/5xx errors.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
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
        self.exports = Exports(self._http, series=self.series)
        self.reference = Reference(self._http)

    def backtest(
        self,
        strategy: Any,
        id: str | list[str],
        *,
        after: Any = None,
        before: Any = None,
        initial_cash: str,
        fees: str | None = "polymarket",
        include_trades: bool = True,
        latency_ms: int = 50,
        slippage_bps: int = 0,
        limit_fill_rate: float = 0.1,
        queue_position: bool = False,
        settlement_delay_ms: int = 5000,
        data_dir: str | None = None,
        **params: Any,
    ) -> Any:
        """Run a backtest on a market, series, or list of markets/series.

        Args:
            data_dir: If set, read market history from local Parquet files
                in this directory instead of fetching from the API.
                Files should be named ``history-{market_id}.parquet``.

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
        )
        engine = BacktestEngine(strategy, config)
        return engine.run(self, id, after=after, before=before, data_dir=data_dir, **params)

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
        timeout: Request timeout in seconds.
        max_retries: Max retries on 429/5xx errors.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
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
        self.exports = AsyncExports(self._http, series=self.series)
        self.reference = AsyncReference(self._http)

    async def backtest(
        self,
        strategy: Any,
        id: str | list[str],
        *,
        after: Any = None,
        before: Any = None,
        initial_cash: str,
        fees: str | None = "polymarket",
        include_trades: bool = True,
        latency_ms: int = 50,
        slippage_bps: int = 0,
        limit_fill_rate: float = 0.1,
        queue_position: bool = False,
        settlement_delay_ms: int = 5000,
        **params: Any,
    ) -> Any:
        """Run a backtest on a market, series, or list of markets/series (async)."""
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
        )
        engine = AsyncBacktestEngine(strategy, config)
        return await engine.run(self, id, after=after, before=before, **params)

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> AsyncMarketLens:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
