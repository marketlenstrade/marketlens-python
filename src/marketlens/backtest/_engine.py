from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pyarrow.parquet as pq

from marketlens._base import _coerce_timestamp
from marketlens.exceptions import NotFoundError
from marketlens.backtest._fees import FeeModel, PolymarketFeeModel, ZeroFeeModel
from marketlens.backtest._fills import FillSimulator
from marketlens.backtest._portfolio import Portfolio
from marketlens.backtest._results import BacktestResult
from marketlens.backtest._strategy import Strategy, StrategyContext
from marketlens.backtest._types import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SettlementRecord,
)
from marketlens.helpers.merge import (
    async_merge_streams,
    merge_streams,
)
from marketlens.helpers.replay import AsyncOrderBookReplay, OrderBookReplay
from marketlens.types.history import DeltaEvent, HistoryEvent, SnapshotEvent, TradeEvent
from marketlens.types.market import Market
from marketlens.types.orderbook import OrderBook, PriceLevel

_FOUR = Decimal("0.0001")


def _iter_history_parquet(path: Path) -> Iterator[HistoryEvent]:
    """Read a history Parquet file and yield HistoryEvent objects."""
    import pandas as pd

    df = pd.read_parquet(path)

    event_types = df["event_type"].values
    ts = df["t"].values
    prices = df["price"].values
    sizes = df["size"].values
    sides = df["side"].values
    trade_ids = df["trade_id"].values
    is_reseeds = df["is_reseed"].values
    bids_col = df["bids"].values
    asks_col = df["asks"].values

    for i in range(len(df)):
        et = event_types[i]
        t = int(ts[i])
        if et == "snapshot":
            bids_raw = json.loads(bids_col[i])
            asks_raw = json.loads(asks_col[i])
            bids = [PriceLevel(price=b["price"], size=b["size"]) for b in bids_raw]
            asks = [PriceLevel(price=a["price"], size=a["size"]) for a in asks_raw]
            yield SnapshotEvent(t=t, is_reseed=bool(is_reseeds[i]), bids=bids, asks=asks)
        elif et == "delta":
            yield DeltaEvent(
                t=t, price=f"{prices[i]:.4f}", size=f"{sizes[i]:.4f}", side=sides[i],
            )
        elif et == "trade":
            yield TradeEvent(
                t=t, id=trade_ids[i], price=f"{prices[i]:.4f}", size=f"{sizes[i]:.4f}", side=sides[i],
            )


@dataclass
class BacktestConfig:
    initial_cash: str = "10000.0000"
    fee_model: FeeModel | None = None
    fees: str | None = "polymarket"
    taker_only: bool = True
    max_fill_fraction: float = 1.0
    include_trades: bool = True
    latency_ms: int = 50
    slippage_bps: int = 0
    limit_fill_rate: float = 0.1
    queue_position: bool = False
    settlement_delay_ms: int = 5000  # on-chain balance availability (~5s after MATCHED)


class _EngineCore:
    """Shared logic for sync and async engines."""

    def __init__(self, strategy: Strategy, config: BacktestConfig | None = None) -> None:
        self._strategy = strategy
        self._config = config or BacktestConfig()

        self._auto_fees = self._config.fees == "polymarket"
        fee_model = self._config.fee_model or ZeroFeeModel()
        self._fill_sim = FillSimulator(
            fee_model,
            taker_only=self._config.taker_only,
            max_fill_fraction=self._config.max_fill_fraction,
            slippage_bps=self._config.slippage_bps,
            limit_fill_rate=self._config.limit_fill_rate,
            queue_position=self._config.queue_position,
        )
        self._latency_ms = self._config.latency_ms
        self._settlement_delay_ms = self._config.settlement_delay_ms
        self._portfolio = Portfolio(self._config.initial_cash)
        self._order_counter = 0
        self._orders: list[Order] = []
        self._open_orders: list[Order] = []
        self._pending_orders: list[tuple[int, Order]] = []  # (activate_at, order)
        # Per-market settlement: earliest time a SELL can activate after a BUY fill
        self._settled_at: dict[str, int] = {}  # market_id → timestamp_ms
        self._settlements: list[SettlementRecord] = []
        self._equity_curve: list[dict] = []
        self._cash_rejected = 0

        self._current_market: Market | None = None
        self._current_book: OrderBook | None = None
        self._current_time: int = 0
        self._books: dict[str, OrderBook] = {}
        self._market_series: dict[str, str] = {}  # market_id → series_id (for settlement attribution)
        self._market_group: dict[str, str] = {}    # market_id → group key (for sequential slot tracking)
        self._ref_prices: dict[str, list[tuple[int, str]]] = {}  # symbol → sorted (timestamp, price)
        self._market_underlying: dict[str, str | None] = {}  # market_id → underlying symbol

        self._ctx = StrategyContext(self)

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    @property
    def current_market(self) -> Market:
        return self._current_market  # type: ignore[return-value]

    @property
    def current_book(self) -> OrderBook:
        return self._current_book  # type: ignore[return-value]

    @property
    def current_time(self) -> int:
        return self._current_time

    @property
    def open_orders(self) -> list[Order]:
        return [o for o in self._open_orders if o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)]

    def submit_order(
        self,
        side: OrderSide,
        size: str,
        *,
        market_id: str | None = None,
        limit_price: str | None = None,
        cancel_after: int | None = None,
    ) -> Order:
        target = market_id or self._current_market.id  # type: ignore[union-attr]
        self._order_counter += 1
        order_type = OrderType.LIMIT if limit_price is not None else OrderType.MARKET

        # Validate sell orders
        if side in (OrderSide.SELL_YES, OrderSide.SELL_NO):
            pos = self._portfolio.position(target)
            expected_side = PositionSide.YES if side == OrderSide.SELL_YES else PositionSide.NO
            held = Decimal(pos.shares) if pos.side == expected_side else Decimal("0")
            needed = Decimal(size)
            if held < needed:
                side_name = "YES" if side == OrderSide.SELL_YES else "NO"
                raise ValueError(
                    f"Cannot sell {size} {side_name} shares: only holding {held.quantize(_FOUR)}"
                )

        # Validate limit price
        if limit_price is not None:
            lp = Decimal(limit_price)
            if lp <= 0 or lp >= 1:
                raise ValueError(f"Limit price must be in (0, 1), got {limit_price}")

        order = Order(
            id=f"ord-{self._order_counter}",
            market_id=target,
            side=side,
            order_type=order_type,
            size=size,
            limit_price=limit_price,
            submitted_at=self._current_time,
            cancel_after=cancel_after,
        )
        self._orders.append(order)

        # Compute activation time: network latency + settlement delay for SELLs
        activate_at = self._current_time + self._latency_ms
        if side in (OrderSide.SELL_YES, OrderSide.SELL_NO):
            settled_at = self._settled_at.get(target, 0)
            activate_at = max(activate_at, settled_at)

        if activate_at > self._current_time:
            self._pending_orders.append((activate_at, order))
        elif order_type == OrderType.MARKET:
            self._fill_market_order(order)
        else:
            order.status = OrderStatus.OPEN
            self._open_orders.append(order)
            self._fill_sim.register_limit_order(order, self._current_book)

        return order

    def cancel_order(self, order: Order) -> None:
        if order.status in (OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
            order.status = OrderStatus.CANCELLED
            self._fill_sim.unregister_order(order.id)
            self._open_orders = [o for o in self._open_orders if o.id != order.id]
            self._pending_orders = [(t, o) for t, o in self._pending_orders if o.id != order.id]

    def cancel_all_orders(self, *, market_id: str | None = None) -> None:
        remaining: list[Order] = []
        for o in self._open_orders:
            if o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED) and (
                market_id is None or o.market_id == market_id
            ):
                o.status = OrderStatus.CANCELLED
                self._fill_sim.unregister_order(o.id)
            else:
                remaining.append(o)
        self._open_orders = remaining
        remaining_pending: list[tuple[int, Order]] = []
        for t, o in self._pending_orders:
            if o.status == OrderStatus.PENDING and (
                market_id is None or o.market_id == market_id
            ):
                o.status = OrderStatus.CANCELLED
            else:
                remaining_pending.append((t, o))
        self._pending_orders = remaining_pending

    def _activate_pending_orders(self, *, market_id: str | None = None) -> None:
        """Activate orders whose latency delay has elapsed.

        When *market_id* is given, only orders for that market are considered.
        This prevents cross-market fills in event (multi-market) mode.
        """
        still_pending: list[tuple[int, Order]] = []
        for activate_at, order in self._pending_orders:
            if (
                self._current_time >= activate_at
                and order.status == OrderStatus.PENDING
                and (market_id is None or order.market_id == market_id)
            ):
                try:
                    if order.order_type == OrderType.MARKET:
                        self._fill_market_order(order)
                    else:
                        order.status = OrderStatus.OPEN
                        self._open_orders.append(order)
                        self._fill_sim.register_limit_order(
                            order, self._books.get(order.market_id, self._current_book),
                        )
                except ValueError:
                    # Position no longer sufficient (e.g. duplicate sell from latency)
                    order.status = OrderStatus.CANCELLED
            else:
                still_pending.append((activate_at, order))
        self._pending_orders = still_pending

    def _fill_market_order(self, order: Order) -> None:
        book = self._books.get(order.market_id, self._current_book)
        fill = self._fill_sim.try_fill_market_order(
            order, book, self._current_time,  # type: ignore[arg-type]
        )
        if fill is None:
            order.status = OrderStatus.CANCELLED
            return
        try:
            self._apply_fill(order, fill)
        except ValueError:
            order.status = OrderStatus.CANCELLED

    def _try_fill_limit_orders(self, trade: TradeEvent) -> list[Fill]:
        fills: list[Fill] = []
        for order in list(self._open_orders):
            if order.status not in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                continue
            if order.market_id != self._current_market.id:  # type: ignore[union-attr]
                continue
            fill = self._fill_sim.try_fill_limit_order(
                order, self._current_book, trade, self._current_time,  # type: ignore[arg-type]
            )
            if fill is None:
                continue
            try:
                self._apply_fill(order, fill)
                fills.append(fill)
            except ValueError:
                order.status = OrderStatus.CANCELLED
                self._open_orders = [o for o in self._open_orders if o.id != order.id]
                self._fill_sim.unregister_order(order.id)
        return fills

    def _apply_fill(self, order: Order, fill: Fill) -> None:
        # Check cash sufficiency for buy orders
        if fill.side in (OrderSide.BUY_YES, OrderSide.BUY_NO):
            cost = Decimal(fill.price) * Decimal(fill.size) + Decimal(fill.fee)
            if self._portfolio._cash < cost:
                self._cash_rejected += 1
                raise ValueError("Insufficient cash")
            # Record when settlement completes (tokens become sellable)
            if self._settlement_delay_ms > 0:
                self._settled_at[fill.market_id] = fill.timestamp + self._settlement_delay_ms
        # Apply to portfolio — may also raise ValueError for insufficient shares
        self._portfolio.apply_fill(fill)

        order.fills.append(fill)
        filled = Decimal(order.filled_size) + Decimal(fill.size)
        order.filled_size = str(filled.quantize(_FOUR))
        order.total_fees = str(
            (Decimal(order.total_fees) + Decimal(fill.fee)).quantize(_FOUR)
        )

        total_cost = sum(Decimal(f.price) * Decimal(f.size) for f in order.fills)
        total_filled = sum(Decimal(f.size) for f in order.fills)
        order.avg_fill_price = str((total_cost / total_filled).quantize(_FOUR))

        if filled >= Decimal(order.size):
            order.status = OrderStatus.FILLED
            self._open_orders = [o for o in self._open_orders if o.id != order.id]
            self._fill_sim.unregister_order(order.id)
        else:
            order.status = OrderStatus.PARTIALLY_FILLED

        self._strategy.on_fill(self._ctx, self._current_market, fill)  # type: ignore[arg-type]

    def _expire_orders(self) -> None:
        remaining: list[Order] = []
        for order in self._open_orders:
            if (
                order.cancel_after is not None
                and self._current_time >= order.cancel_after
            ):
                order.status = OrderStatus.EXPIRED
                self._fill_sim.unregister_order(order.id)
            else:
                remaining.append(order)
        self._open_orders = remaining

    def _process_event(self, event: SnapshotEvent | DeltaEvent | TradeEvent, book: OrderBook, market: Market, first_book_seen: bool) -> bool:
        """Process a single event. Returns True if this was the first book event."""
        self._current_market = market
        self._current_book = book
        self._current_time = event.t
        self._books[market.id] = book
        is_first = False

        self._activate_pending_orders(market_id=market.id)

        if isinstance(event, TradeEvent):
            self._try_fill_limit_orders(event)
            self._strategy.on_trade(self._ctx, market, book, event)
        elif isinstance(event, (SnapshotEvent, DeltaEvent)):
            if isinstance(event, DeltaEvent):
                self._fill_sim.notify_delta(market.id, event.price, event.size, event.side)
            else:
                self._fill_sim.notify_snapshot(market.id, book)
            if not first_book_seen:
                self._strategy.on_market_start(self._ctx, market, book)
                is_first = True
            self._strategy.on_book(self._ctx, market, book)

        self._expire_orders()
        self._portfolio.mark_to_market(market.id, book)

        if isinstance(event, SnapshotEvent):
            equity = self._portfolio.equity
            pnl = str((Decimal(equity) - Decimal(self._portfolio.initial_cash)).quantize(_FOUR))
            self._equity_curve.append({
                "t": event.t,
                "market_id": market.id,
                "cash": self._portfolio.cash,
                "equity": equity,
                "pnl": pnl,
            })

        return is_first

    def _finalize_market(self, market: Market) -> None:
        self._strategy.on_market_end(self._ctx, market)
        self.cancel_all_orders(market_id=market.id)

        if market.status == "resolved" and market.winning_outcome_index is not None:
            timestamp = market.resolved_at or market.close_time or self._current_time
            series_id = self._market_series.get(market.id)
            record = self._portfolio.settle_market(market, timestamp, series_id=series_id)
            if record is not None:
                self._settlements.append(record)

        self._books.pop(market.id, None)

    def _run_merged(
        self,
        streams: list[Iterator[tuple[Market, HistoryEvent, OrderBook]]],
    ) -> None:
        first_book_seen: set[str] = set()
        active: dict[str, Market] = {}  # grouping_key → current Market

        for market, event, book in merge_streams(streams):
            key = self._market_group.get(market.id, market.id)

            # Market transition: previous market in this slot ended
            prev = active.get(key)
            if prev is not None and prev.id != market.id:
                self._finalize_market(prev)
            active[key] = market

            if self._auto_fees:
                self._fill_sim._fee_model = PolymarketFeeModel.for_category(market.category)

            seen = market.id in first_book_seen
            if self._process_event(event, book, market, seen):
                first_book_seen.add(market.id)
            elif not seen and isinstance(event, (SnapshotEvent, DeltaEvent)):
                first_book_seen.add(market.id)

        # Finalize remaining
        for m in active.values():
            self._finalize_market(m)

    def _make_market_stream(
        self,
        client: Any,
        markets: list[Market],
        *,
        after: Any = None,
        before: Any = None,
    ) -> Iterator[tuple[Market, HistoryEvent, OrderBook]]:
        """Chain sequential markets from one series into a single lazy event stream."""
        history_params: dict[str, Any] = {}
        if self._config.include_trades:
            history_params["include_trades"] = True

        for market in markets:
            history = client.orderbook.history(
                market.id,
                after=after or market.open_time,
                before=before or market.close_time,
                **history_params,
            )
            replay = OrderBookReplay(history, market_id=market.id, platform=market.platform)
            for event, book in replay:
                yield market, event, book

    def _make_file_stream(
        self,
        markets: list[Market],
        data_dir: str,
    ) -> Iterator[tuple[Market, HistoryEvent, OrderBook]]:
        """Read market history from local Parquet files instead of the API."""
        for market in markets:
            path = Path(data_dir) / f"history-{market.id}.parquet"
            if not path.exists():
                import warnings
                warnings.warn(f"Skipping market {market.id}: {path} not found")
                continue
            events = _iter_history_parquet(path)
            replay = OrderBookReplay(events, market_id=market.id, platform=market.platform)
            for event, book in replay:
                yield market, event, book

    def get_reference_price(self, symbol: str | None, at_time: int) -> str | None:
        if symbol is None or symbol not in self._ref_prices:
            return None
        prices = self._ref_prices[symbol]
        if not prices:
            return None
        # Each entry is a completed trade at its exact timestamp.
        # Return the most recent trade at or before at_time.
        idx = bisect.bisect_right(prices, (at_time, "~")) - 1
        return prices[idx][1] if idx >= 0 else None

    def _register_market(self, market: Market) -> None:
        self._market_underlying[market.id] = market.underlying

    def _build_result(self) -> BacktestResult:
        return BacktestResult(
            portfolio=self._portfolio,
            orders=self._orders,
            settlements=self._settlements,
            equity_curve=self._equity_curve,
            cash_rejected=self._cash_rejected,
        )



class BacktestEngine(_EngineCore):
    def _prefetch_reference_prices(self, data_dir: str | None = None) -> None:
        underlyings = {u for u in self._market_underlying.values() if u is not None}
        for symbol in underlyings:
            if symbol in self._ref_prices:
                continue
            if data_dir is None:
                raise ValueError(
                    f"data_dir is required to load reference trades for {symbol}. "
                    "Use client.exports.download_series() to download data first."
                )
            ref_path = Path(data_dir) / f"reference-{symbol}.parquet"
            if not ref_path.exists():
                raise FileNotFoundError(
                    f"Reference trades not found: {ref_path}. "
                    "Re-run client.exports.download_series() to download tick data."
                )
            table = pq.read_table(ref_path, columns=["timestamp", "price"])
            ts_col = table.column("timestamp").to_pylist()
            price_col = [str(v) for v in table.column("price").to_pylist()]
            self._ref_prices[symbol] = list(zip(ts_col, price_col))

    def run(
        self,
        client: Any,
        id: str | list[str],
        *,
        after: Any = None,
        before: Any = None,
        data_dir: str | None = None,
        **params: Any,
    ) -> BacktestResult:
        def _stream(markets: list[Market]) -> Iterator[tuple[Market, HistoryEvent, OrderBook]]:
            if data_dir is not None:
                return self._make_file_stream(markets, data_dir)
            return self._make_market_stream(client, markets, after=after, before=before)

        if isinstance(id, list):
            streams = self._resolve_list(client, id, after=after, before=before, data_dir=data_dir, **params)
            self._prefetch_reference_prices(data_dir=data_dir)
            self._run_merged(streams)
            return self._build_result()

        # 1. Try as a market UUID
        try:
            market = client.markets.get(id)
            self._market_series[market.id] = market.series_id or market.id
            self._register_market(market)
            self._prefetch_reference_prices(data_dir=data_dir)
            self._run_merged([_stream([market])])
            return self._build_result()
        except NotFoundError:
            pass

        # 2. Try as a series
        try:
            series = client.series.get(id)
        except NotFoundError:
            series = None

        if series is not None:
            if series.structured_type:
                streams = self._resolve_structured(
                    client, id, series, after=after, before=before, data_dir=data_dir, **params,
                )
                self._prefetch_reference_prices(data_dir=data_dir)
                self._run_merged(streams)
            elif series.is_rolling:
                markets = list(client.series.walk(id, after=after, before=before, **params))
                for m in markets:
                    self._market_series[m.id] = series.id
                    self._market_group[m.id] = series.id
                    self._register_market(m)
                self._prefetch_reference_prices(data_dir=data_dir)
                self._run_merged([_stream(markets)])
            else:
                raise ValueError(
                    f"Series '{series.title}' is neither rolling nor structured."
                )
            return self._build_result()

        # 3. Fallback: condition ID
        found = client.markets.list(condition_id=id).to_list()
        if found:
            self._market_series[found[0].id] = found[0].series_id or found[0].id
            self._register_market(found[0])
            self._prefetch_reference_prices(data_dir=data_dir)
            self._run_merged([_stream([found[0]])])
            return self._build_result()

        raise NotFoundError(404, "NOT_FOUND", f"No market or series found for '{id}'")

    def _resolve_list(
        self,
        client: Any,
        ids: list[str],
        *,
        after: Any = None,
        before: Any = None,
        data_dir: str | None = None,
        **params: Any,
    ) -> list[Iterator[tuple[Market, HistoryEvent, OrderBook]]]:
        def _stream(markets: list[Market]) -> Iterator[tuple[Market, HistoryEvent, OrderBook]]:
            if data_dir is not None:
                return self._make_file_stream(markets, data_dir)
            return self._make_market_stream(client, markets, after=after, before=before)

        streams: list[Iterator[tuple[Market, HistoryEvent, OrderBook]]] = []
        for item_id in ids:
            # Try market UUID
            try:
                market = client.markets.get(item_id)
                self._market_series[market.id] = market.series_id or market.id
                self._register_market(market)
                streams.append(_stream([market]))
                continue
            except NotFoundError:
                pass

            # Try series
            series = client.series.get(item_id)
            if series.structured_type:
                streams.extend(self._resolve_structured(
                    client, item_id, series, after=after, before=before, data_dir=data_dir, **params,
                ))
            elif series.is_rolling:
                markets = list(client.series.walk(item_id, after=after, before=before, **params))
                for m in markets:
                    self._market_series[m.id] = series.id
                    self._market_group[m.id] = series.id
                    self._register_market(m)
                streams.append(_stream(markets))
            else:
                raise ValueError(
                    f"Series '{series.title}' is neither rolling nor structured."
                )

        return streams

    def _resolve_structured(
        self,
        client: Any,
        series_id: str,
        series: Any,
        *,
        after: Any = None,
        before: Any = None,
        data_dir: str | None = None,
        **params: Any,
    ) -> list[Iterator[tuple[Market, HistoryEvent, OrderBook]]]:
        """Resolve a structured series into per-market streams."""
        from marketlens._base import _coerce_timestamp

        def _stream(markets: list[Market]) -> Iterator[tuple[Market, HistoryEvent, OrderBook]]:
            if data_dir is not None:
                return self._make_file_stream(markets, data_dir)
            return self._make_market_stream(client, markets, after=after, before=before)

        event_params = dict(params)
        if after is not None:
            event_params["end_after"] = after
        if before is not None:
            event_params["start_before"] = before
        events = client.series.events(series_id, **event_params).to_list()

        after_ms = _coerce_timestamp(after) if after is not None else None
        before_ms = _coerce_timestamp(before) if before is not None else None

        streams: list[Iterator[tuple[Market, HistoryEvent, OrderBook]]] = []
        for evt in events:
            event_markets = client.events.markets(evt.id).to_list()
            for m in event_markets:
                if after_ms is not None and m.close_time and m.close_time < after_ms:
                    continue
                if before_ms is not None and m.open_time and m.open_time > before_ms:
                    continue
                self._market_series[m.id] = series.id
                self._register_market(m)
                streams.append(_stream([m]))
        return streams


class AsyncBacktestEngine(_EngineCore):
    async def _async_prefetch_reference_prices(self, data_dir: str | None = None) -> None:
        underlyings = {u for u in self._market_underlying.values() if u is not None}
        for symbol in underlyings:
            if symbol in self._ref_prices:
                continue
            if data_dir is None:
                raise ValueError(
                    f"data_dir is required to load reference trades for {symbol}. "
                    "Use client.exports.download_series() to download data first."
                )
            ref_path = Path(data_dir) / f"reference-{symbol}.parquet"
            if not ref_path.exists():
                raise FileNotFoundError(
                    f"Reference trades not found: {ref_path}. "
                    "Re-run client.exports.download_series() to download tick data."
                )
            table = pq.read_table(ref_path, columns=["timestamp", "price"])
            ts_col = table.column("timestamp").to_pylist()
            price_col = [str(v) for v in table.column("price").to_pylist()]
            self._ref_prices[symbol] = list(zip(ts_col, price_col))

    async def run(
        self,
        client: Any,
        id: str | list[str],
        *,
        after: Any = None,
        before: Any = None,
        data_dir: str | None = None,
        **params: Any,
    ) -> BacktestResult:
        if isinstance(id, list):
            streams = await self._resolve_list(client, id, after=after, before=before, **params)
            await self._async_prefetch_reference_prices(data_dir=data_dir)
            await self._run_merged(streams)
            return self._build_result()

        # 1. Try as a market UUID
        try:
            market = await client.markets.get(id)
            self._market_series[market.id] = market.series_id or market.id
            self._register_market(market)
            await self._async_prefetch_reference_prices(data_dir=data_dir)
            await self._run_merged([self._async_make_market_stream(client, [market], after=after, before=before)])
            return self._build_result()
        except NotFoundError:
            pass

        # 2. Try as a series
        try:
            series = await client.series.get(id)
        except NotFoundError:
            series = None

        if series is not None:
            if series.structured_type:
                streams = await self._async_resolve_structured(
                    client, id, series, after=after, before=before, **params,
                )
                await self._async_prefetch_reference_prices(data_dir=data_dir)
                await self._run_merged(streams)
            elif series.is_rolling:
                markets = []
                async for m in client.series.walk(id, after=after, before=before, **params):
                    markets.append(m)
                for m in markets:
                    self._market_series[m.id] = series.id
                    self._market_group[m.id] = series.id
                    self._register_market(m)
                await self._async_prefetch_reference_prices(data_dir=data_dir)
                await self._run_merged([self._async_make_market_stream(client, markets, after=after, before=before)])
            else:
                raise ValueError(
                    f"Series '{series.title}' is neither rolling nor structured."
                )
            return self._build_result()

        # 3. Fallback: condition ID
        found = await client.markets.list(condition_id=id).to_list()
        if found:
            self._market_series[found[0].id] = found[0].series_id or found[0].id
            self._register_market(found[0])
            await self._async_prefetch_reference_prices(data_dir=data_dir)
            await self._run_merged([self._async_make_market_stream(client, [found[0]], after=after, before=before)])
            return self._build_result()

        raise NotFoundError(404, "NOT_FOUND", f"No market or series found for '{id}'")

    async def _resolve_list(
        self,
        client: Any,
        ids: list[str],
        *,
        after: Any = None,
        before: Any = None,
        **params: Any,
    ) -> list[AsyncIterator[tuple[Market, HistoryEvent, OrderBook]]]:
        streams: list[AsyncIterator[tuple[Market, HistoryEvent, OrderBook]]] = []
        for item_id in ids:
            # Try market UUID
            try:
                market = await client.markets.get(item_id)
                self._market_series[market.id] = market.series_id or market.id
                self._register_market(market)
                streams.append(self._async_make_market_stream(client, [market], after=after, before=before))
                continue
            except NotFoundError:
                pass

            # Try series
            series = await client.series.get(item_id)
            if series.structured_type:
                streams.extend(await self._async_resolve_structured(
                    client, item_id, series, after=after, before=before, **params,
                ))
            elif series.is_rolling:
                markets = []
                async for m in client.series.walk(item_id, after=after, before=before, **params):
                    markets.append(m)
                for m in markets:
                    self._market_series[m.id] = series.id
                    self._market_group[m.id] = series.id
                    self._register_market(m)
                streams.append(self._async_make_market_stream(client, markets, after=after, before=before))
            else:
                raise ValueError(
                    f"Series '{series.title}' is neither rolling nor structured."
                )

        return streams

    async def _async_make_market_stream(
        self,
        client: Any,
        markets: list[Market],
        *,
        after: Any = None,
        before: Any = None,
    ) -> AsyncIterator[tuple[Market, HistoryEvent, OrderBook]]:
        """Async version of ``_make_market_stream``."""
        history_params: dict[str, Any] = {}
        if self._config.include_trades:
            history_params["include_trades"] = True

        for market in markets:
            history = client.orderbook.history(
                market.id,
                after=after or market.open_time,
                before=before or market.close_time,
                **history_params,
            )
            replay = AsyncOrderBookReplay(history, market_id=market.id, platform=market.platform)
            async for event, book in replay:
                yield market, event, book

    async def _run_merged(  # type: ignore[override]
        self,
        streams: list[AsyncIterator[tuple[Market, HistoryEvent, OrderBook]]],
    ) -> None:
        first_book_seen: set[str] = set()
        active: dict[str, Market] = {}

        async for market, event, book in async_merge_streams(streams):
            key = self._market_group.get(market.id, market.id)
            prev = active.get(key)
            if prev is not None and prev.id != market.id:
                self._finalize_market(prev)
            active[key] = market

            if self._auto_fees:
                self._fill_sim._fee_model = PolymarketFeeModel.for_category(market.category)

            seen = market.id in first_book_seen
            if self._process_event(event, book, market, seen):
                first_book_seen.add(market.id)
            elif not seen and isinstance(event, (SnapshotEvent, DeltaEvent)):
                first_book_seen.add(market.id)

        for m in active.values():
            self._finalize_market(m)

    async def _async_resolve_structured(
        self,
        client: Any,
        series_id: str,
        series: Any,
        *,
        after: Any = None,
        before: Any = None,
        **params: Any,
    ) -> list[AsyncIterator[tuple[Market, HistoryEvent, OrderBook]]]:
        """Resolve a structured series into per-market async streams."""
        from marketlens._base import _coerce_timestamp

        event_params = dict(params)
        if after is not None:
            event_params["end_after"] = after
        if before is not None:
            event_params["start_before"] = before
        events = await (await client.series.events(series_id, **event_params)).to_list()

        after_ms = _coerce_timestamp(after) if after is not None else None
        before_ms = _coerce_timestamp(before) if before is not None else None

        streams: list[AsyncIterator[tuple[Market, HistoryEvent, OrderBook]]] = []
        for evt in events:
            event_markets = await client.events.markets(evt.id).to_list()
            for m in event_markets:
                if after_ms is not None and m.close_time and m.close_time < after_ms:
                    continue
                if before_ms is not None and m.open_time and m.open_time > before_ms:
                    continue
                self._market_series[m.id] = series.id
                self._register_market(m)
                streams.append(self._async_make_market_stream(
                    client, [m], after=after, before=before,
                ))
        return streams
