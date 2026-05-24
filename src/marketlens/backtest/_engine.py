from __future__ import annotations

import bisect
import json
import os
import sys
import warnings
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pyarrow.parquet as pq

from marketlens._base import _coerce_timestamp
from marketlens._progress import _ProgressReporter, make_reporter
from marketlens.exceptions import NotFoundError
from marketlens.backtest._fees import FeeModel, PolymarketFeeModel, ZeroFeeModel
from marketlens.backtest._fills import FillSimulator
from marketlens.backtest._portfolio import Portfolio
from marketlens.backtest._prefetch import AsyncPrefetchedIterator, PrefetchedIterator
from marketlens.backtest._results import BacktestResult
from marketlens.backtest._strategy import Strategy, StrategyContext, _is_trade_only
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


def _prep_status(message: str) -> None:
    """One-line status to stderr before the reporter context is active.
    Suppressed when progress is disabled via env var."""
    if os.environ.get("MARKETLENS_PROGRESS", "").strip().lower() in {"0", "false", "no", "off"}:
        return
    try:
        sys.stderr.write(f"· {message}\n")
        sys.stderr.flush()
    except Exception:
        pass
from marketlens.types.market import Market
from marketlens.types.orderbook import OrderBook, PriceLevel

_FOUR = Decimal("0.0001")


def _pack_into_lanes(markets: list[Market]) -> list[list[Market]]:
    """Pack markets into time-disjoint lanes via greedy interval coloring.

    Each lane becomes one ``_make_market_stream`` chain. Overlapping
    markets go into separate lanes, so the lane count equals the peak
    concurrent market count — bounding the prefetcher count regardless
    of total market count. Markets without ``open_time``/``close_time``
    are isolated (no overlap info to reason about).
    """
    timed = [m for m in markets if m.open_time is not None and m.close_time is not None]
    untimed = [m for m in markets if m.open_time is None or m.close_time is None]

    timed.sort(key=lambda m: m.open_time)  # type: ignore[arg-type]
    lanes: list[list[Market]] = []
    lanes_last_close: list[int] = []

    for m in timed:
        placed = False
        for i, last_close in enumerate(lanes_last_close):
            if last_close <= m.open_time:  # type: ignore[operator]
                lanes[i].append(m)
                lanes_last_close[i] = m.close_time  # type: ignore[assignment]
                placed = True
                break
        if not placed:
            lanes.append([m])
            lanes_last_close.append(m.close_time)  # type: ignore[arg-type]

    for m in untimed:
        lanes.append([m])
    return lanes


def _iter_history_parquet(path: Path) -> Iterator[HistoryEvent]:
    """Read a history Parquet file and yield HistoryEvent objects."""
    import pandas as pd

    df = pd.read_parquet(path)

    # PyArrow-backed columns route every .values[i] through arrow __getitem__
    # (~2.7us, ~970K calls/market). .tolist() materializes once into a native
    # Python list with C-level O(1) indexing.
    event_types = df["event_type"].tolist()
    ts = df["t"].tolist()
    prices = df["price"].tolist()
    sizes = df["size"].tolist()
    sides = df["side"].tolist()
    trade_ids = df["trade_id"].tolist()
    is_reseeds = df["is_reseed"].tolist()
    bids_col = df["bids"].tolist()
    asks_col = df["asks"].tolist()

    # Reorder dispatch: deltas + trades dominate; snapshot is rare (~16/market).
    for i in range(len(event_types)):
        et = event_types[i]
        t = int(ts[i])
        if et == "delta":
            yield DeltaEvent(
                t=t, price=f"{prices[i]:.4f}", size=f"{sizes[i]:.4f}", side=sides[i],
            )
        elif et == "trade":
            yield TradeEvent(
                t=t, id=trade_ids[i], price=f"{prices[i]:.4f}", size=f"{sizes[i]:.4f}", side=sides[i],
            )
        elif et == "snapshot":
            bids_raw = json.loads(bids_col[i])
            asks_raw = json.loads(asks_col[i])
            bids = [PriceLevel(price=b["price"], size=b["size"]) for b in bids_raw]
            asks = [PriceLevel(price=a["price"], size=a["size"]) for a in asks_raw]
            yield SnapshotEvent(t=t, is_reseed=bool(is_reseeds[i]), bids=bids, asks=asks)


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
    progress: bool = True  # show rich progress bars for fetch/backtest
    # None=auto, True=force compact, False=force full. Auto picks compact
    # when on_book isn't overridden and queue_position/include_trades allow it.
    coalesce: bool | None = None


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

        self._targets: dict[str, Any] = {}

        self._current_market: Market | None = None
        self._current_book: OrderBook | None = None
        self._current_time: int = 0
        self._books: dict[str, OrderBook] = {}
        # Latest Market object per id — needed when pending orders activate
        # between events (their fill is stamped against their own market, not
        # the market whose event triggered the drain).
        self._market_objs: dict[str, Market] = {}
        self._market_series: dict[str, str] = {}  # market_id → series_id (for settlement attribution)
        self._market_group: dict[str, str] = {}    # market_id → group key (for sequential slot tracking)
        self._ref_prices: dict[str, list[tuple[int, str]]] = {}  # symbol → sorted (timestamp, price)
        self._market_underlying: dict[str, str | None] = {}  # market_id → underlying symbol
        self._underlying_bounds: dict[str, tuple[int, int]] = {}  # symbol → (earliest_open, latest_close)
        # Set in run() so get_reference_price() can lazily load on first use.
        # Strategies that never call ctx.reference_price() pay zero load cost.
        self._ref_load_ctx: dict[str, Any] = {}

        # Set by run() inside a `with reporter:` block. No-op outside.
        self._reporter: _ProgressReporter = make_reporter(enabled=False)

        self._compact_mode = self._resolve_compact_mode()

        self._ctx = StrategyContext(self)

    def _resolve_compact_mode(self) -> bool:
        """Decide whether to use the trade-aligned compact data path.

        Honours an explicit ``config.coalesce`` override, otherwise
        auto-detects from the strategy's hook signature.
        """
        compatible = (
            not self._config.queue_position and self._config.include_trades
        )
        override = self._config.coalesce
        if override is True:
            if not compatible:
                reason = ("queue_position=True" if self._config.queue_position
                          else "include_trades=False")
                raise ValueError(
                    f"coalesce=True is incompatible with {reason}."
                )
            return True
        if override is False:
            return False
        return _is_trade_only(self._strategy) and compatible

    def _resolve_history_file(self, data_dir: Path, market_id: str) -> Path | None:
        """Pick the history parquet variant for ``market_id``.

        Prefers the variant matching the strategy mode. Falls back to the
        other one with a stderr note when correctness is preserved; hard-
        errors when ``queue_position=True`` and only the compact file is
        present (compact lacks the per-delta detail queue tracking needs).
        """
        full = data_dir / f"history-{market_id}.parquet"
        compact = data_dir / f"history-{market_id}-compact.parquet"
        preferred, fallback = (compact, full) if self._compact_mode else (full, compact)

        chosen = preferred if preferred.exists() else (
            fallback if fallback.exists() else None
        )
        if chosen is None:
            return None
        if chosen is fallback:
            if self._config.queue_position and chosen == compact:
                raise ValueError(
                    f"queue_position=True requires the full-firehose history "
                    f"file, but only {compact.name} is present in {data_dir}. "
                    f"Re-run client.exports.download(..., coalesce=False)."
                )
            note = (
                "compact data with on_book overridden — book updates fire only at "
                "snapshot and trade boundaries" if not self._compact_mode
                else "full data with trade-only strategy — slower than necessary; "
                "consider re-downloading with coalesce=True"
            )
            try:
                sys.stderr.write(f"· using {chosen.name}: {note}\n")
                sys.stderr.flush()
            except Exception:
                pass
        self._targets.setdefault("resolved_files", {})[market_id] = chosen.name
        return chosen

    def _maybe_autodownload(
        self,
        client: Any,
        id: str | list[str],
        *,
        after: Any,
        before: Any,
        data_dir: str | None,
    ) -> None:
        """Fetch the bulk export when ``data_dir`` is set but empty.

        Called after each run-path's resolution log so the user sees:
        "Resolving markets in '...'" → "Downloading M/N" → "Backtesting M/N".
        """
        if data_dir is None:
            return
        path = Path(data_dir)
        if path.exists() and any(path.glob("history-*.parquet")):
            return
        client._ensure_exports_downloaded(
            id, data_dir,
            after=after, before=before,
            coalesce=self._resolve_compact_mode(),
            progress=self._config.progress,
        )

    def _with_reporter(self, n_markets: int, *, replay: bool = False):
        """Context manager that installs a progress reporter for the run.

        ``replay=True`` tells the reporter to skip the "Fetching" bar
        since the data is already on disk and no network fetch happens.
        """
        engine = self
        config = self._config

        class _Ctx:
            def __enter__(self_inner):
                self_inner.reporter = make_reporter(
                    enabled=config.progress, n_markets=n_markets,
                )
                self_inner.reporter.__enter__()
                if replay:
                    self_inner.reporter.set_mode("replay")
                self_inner.prev = engine._reporter
                engine._reporter = self_inner.reporter
                return self_inner.reporter

            def __exit__(self_inner, *args):
                engine._reporter = self_inner.prev
                return self_inner.reporter.__exit__(*args)

        return _Ctx()

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
        size: str | float | int | Decimal,
        *,
        market_id: str | None = None,
        limit_price: str | float | int | Decimal | None = None,
        cancel_after: int | None = None,
    ) -> Order:
        # Accept any numeric input from the strategy (float/int/Decimal) and
        # normalize to the decimal-string form the rest of the engine and the
        # Order dataclass expect. ``str(float)`` already round-trips cleanly
        # in CPython (``str(0.1) == "0.1"``).
        if not isinstance(size, str):
            size = str(size)
        if limit_price is not None and not isinstance(limit_price, str):
            limit_price = str(limit_price)

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

        # Latency / settlement gate _when_ the fill is recorded. The price is
        # determined at activation against the live book at that moment, so
        # an order in flight is exposed to the book moves that happen during
        # the latency window (the standard adverse-selection modelling).
        activate_at = self._current_time + self._latency_ms
        if side in (OrderSide.SELL_YES, OrderSide.SELL_NO):
            activate_at = max(activate_at, self._settled_at.get(target, 0))

        if activate_at > self._current_time:
            self._pending_orders.append((activate_at, order))
        elif order_type == OrderType.MARKET:
            self._fill_market_order(order)
        else:
            self._activate_limit_order(order)

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

    def _reject_order(self, order: Order) -> None:
        """Mark an engine-side rejection, clean up, fire ``on_reject``.

        Distinct from user-initiated ``cancel_order``: this fires when the
        engine itself decides the order cannot proceed (empty book, insufficient
        cash/shares at activation, etc.) so strategies can react.
        """
        order.status = OrderStatus.CANCELLED
        self._fill_sim.unregister_order(order.id)
        self._open_orders = [o for o in self._open_orders if o.id != order.id]
        market = self._market_objs.get(order.market_id) or self._current_market
        if market is not None:
            self._strategy.on_reject(self._ctx, market, order)

    def _activate_pending_orders(self, *, market_id: str | None = None) -> None:
        """Activate orders whose latency delay has elapsed at this event's time.

        Called from :meth:`_process_event` for orders with ``activate_at`` equal
        to the event timestamp — they activate against the just-updated
        post-event book. Orders with ``activate_at`` strictly before the event
        are handled by :meth:`_drain_pending_before`.

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
                        self._activate_limit_order(order)
                except ValueError:
                    # Position no longer sufficient (e.g. duplicate sell from latency)
                    self._reject_order(order)
            else:
                still_pending.append((activate_at, order))
        self._pending_orders = still_pending

    def _drain_pending_before(self, event_t: int) -> None:
        """Activate pending orders scheduled strictly before ``event_t``.

        Each order activates at its own ``activate_at`` timestamp, against the
        live per-market book as it stands right now (the post-state of the
        previous event for that market, unchanged since). This is the standard
        event-driven-simulator semantics: between events the book is a step
        function held flat, so "live book at activate_at" is exactly the most-
        recently-emitted per-market book.

        Pending orders whose market has not yet produced any event (no entry
        in ``self._books``) stay pending — they need at least one book before
        they can be priced.
        """
        if not self._pending_orders:
            return

        ready: list[tuple[int, Order]] = []
        still: list[tuple[int, Order]] = []
        for activate_at, order in self._pending_orders:
            if (
                order.status == OrderStatus.PENDING
                and activate_at < event_t
                and order.market_id in self._books
                and order.market_id in self._market_objs
            ):
                ready.append((activate_at, order))
            else:
                still.append((activate_at, order))

        if not ready:
            return

        # Fire in chronological order so cash-draining buys take effect before
        # subsequent activations see them.
        ready.sort(key=lambda x: x[0])

        for activate_at, order in ready:
            market = self._market_objs[order.market_id]
            self._current_time = activate_at
            self._current_market = market
            self._current_book = self._books[order.market_id]
            if self._auto_fees:
                self._fill_sim._fee_model = PolymarketFeeModel.for_category(market.category)
            try:
                if order.order_type == OrderType.MARKET:
                    self._fill_market_order(order)
                else:
                    self._activate_limit_order(order)
            except ValueError:
                self._reject_order(order)

        self._pending_orders = still

    def _live_book(self, order: Order) -> OrderBook:
        """Most recent per-market book for pricing ``order``.

        Always reflects the live state — there is no submission-time pin. An
        order that activates after a latency delay sees the book as it exists
        at activation, not at submission, which models price drift / depth
        loss while the order was in flight.
        """
        return self._books.get(
            order.market_id, self._current_book,  # type: ignore[arg-type]
        )

    def _activate_limit_order(self, order: Order) -> None:
        """Activate a limit order: fill crossing portion as taker, rest as maker."""
        book = self._live_book(order)
        crossing_fill = self._fill_sim.try_fill_crossing_limit_order(
            order, book, self._current_time,
        )
        if crossing_fill is not None:
            self._apply_fill(order, crossing_fill)

        if Decimal(order.size) - Decimal(order.filled_size) <= Decimal("0.0001"):
            return

        # Rest the remainder. Register against the same live book so the
        # crossing-fill side and the queue-position side share one book state.
        order.status = OrderStatus.OPEN
        self._open_orders.append(order)
        self._fill_sim.register_limit_order(order, book)

    def _fill_market_order(self, order: Order) -> None:
        fill = self._fill_sim.try_fill_market_order(
            order, self._live_book(order), self._current_time,
        )
        if fill is None:
            self._reject_order(order)
            return
        try:
            self._apply_fill(order, fill)
        except ValueError:
            self._reject_order(order)

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
                self._reject_order(order)
        return fills

    def _apply_fill(self, order: Order, fill: Fill) -> None:
        # Check cash sufficiency for buy orders. Net the CTF-merge credit
        # against the cost: buying the opposite side of an existing position
        # nets matched YES+NO pairs back to $1 each (Portfolio.apply_fill).
        # Without this credit, a fully-funded hedge can be falsely rejected
        # purely because the gross buy notional exceeds free cash.
        if fill.side in (OrderSide.BUY_YES, OrderSide.BUY_NO):
            target_side = (
                PositionSide.YES if fill.side == OrderSide.BUY_YES else PositionSide.NO
            )
            opposite = (
                PositionSide.NO if target_side == PositionSide.YES else PositionSide.YES
            )
            pos = self._portfolio.position(fill.market_id)
            matched = (
                min(Decimal(pos.shares), Decimal(fill.size))
                if pos.side == opposite else Decimal("0")
            )
            cost = (
                Decimal(fill.price) * Decimal(fill.size)
                + Decimal(fill.fee)
                - matched
            )
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
        self._market_objs[market.id] = market
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
        finalized: set[str] = set()  # market IDs already finalized

        for market, event, book in merge_streams(streams):
            self._reporter.consumed(market.id, 1)
            # Skip events for markets already finalized (past close_time)
            if market.id in finalized:
                continue

            # Fire any pending orders whose activate_at falls strictly before
            # this event. They price against the live book at their own
            # activate_at — which, between events, is the most-recently-emitted
            # per-market book.
            self._drain_pending_before(event.t)

            key = self._market_group.get(market.id, market.id)

            # Market transition: previous market in this slot ended
            prev = active.get(key)
            if prev is not None and prev.id != market.id:
                self._finalize_market(prev)
                finalized.add(prev.id)
            active[key] = market

            if self._auto_fees:
                self._fill_sim._fee_model = PolymarketFeeModel.for_category(market.category)

            seen = market.id in first_book_seen
            if self._process_event(event, book, market, seen):
                first_book_seen.add(market.id)
            elif not seen and isinstance(event, (SnapshotEvent, DeltaEvent)):
                first_book_seen.add(market.id)

            # Finalize markets that have passed their close_time
            expired = [
                k for k, m in active.items()
                if m.close_time and self._current_time >= m.close_time
                and m.id not in finalized
            ]
            for k in expired:
                self._finalize_market(active[k])
                finalized.add(active[k].id)
                del active[k]

        # Finalize remaining
        for m in active.values():
            if m.id not in finalized:
                self._finalize_market(m)

    def _make_market_stream(
        self,
        client: Any,
        markets: list[Market],
        *,
        after: Any = None,
        before: Any = None,
    ) -> Iterator[tuple[Market, HistoryEvent, OrderBook]]:
        """Stream events from a chronological chain of time-disjoint markets.

        While market[i] is being consumed, market[i+1]'s prefetcher is
        already running so the inter-market network round-trip is
        hidden behind the previous market's tail. The first prefetcher
        starts lazily on first ``next()`` so constructing many streams
        back-to-back doesn't stampede the API.

        Per-market query bounds are clamped to the market's lifetime
        ``[open_time, close_time)`` intersected with the user window so the
        streaming endpoint never returns post-close stale events for one
        market or pre-open events that belong to a neighbouring market.
        Without this clamp, multi-market backtests over a window wider than
        any single market's lifetime see different events in streaming vs
        bulk modes — bulk's parquet is naturally bounded by ``close_time``
        but the streaming endpoint honours the user-supplied ``before``
        verbatim.
        """
        if not markets:
            return

        history_params: dict[str, Any] = {}
        if self._config.include_trades:
            history_params["include_trades"] = True
        if self._compact_mode:
            history_params["coalesce"] = True

        reporter = self._reporter
        user_after_ms = _coerce_timestamp(after)
        user_before_ms = _coerce_timestamp(before)

        def _make_prefetcher(market: Market) -> PrefetchedIterator:
            # Clamp per-market query bounds to ``[open_time, close_time)`` ∩
            # ``[user.after, user.before)``. Snapshot anchor lookup is still
            # extended ``_ANCHOR_LOWER_MARGIN_MS`` past ``open_time`` server-
            # side, so a pre-open anchor is still picked up.
            eff_after = market.open_time if user_after_ms is None else max(
                user_after_ms, market.open_time or user_after_ms,
            )
            eff_before = market.close_time if user_before_ms is None else min(
                user_before_ms, market.close_time or user_before_ms,
            )
            history = client.orderbook.history(
                market.id,
                after=eff_after,
                before=eff_before,
                **history_params,
            )
            mid = market.id
            return PrefetchedIterator(
                history,
                on_fetched=lambda n, mid=mid: reporter.fetched(mid, n),
                on_done=lambda mid=mid: reporter.market_fetch_done(mid),
            )

        current = _make_prefetcher(markets[0]).start()
        next_prefetcher: PrefetchedIterator | None = None
        try:
            for i, market in enumerate(markets):
                # Prime market[i+1] before consuming market[i] so the next
                # market's first page is fetched in parallel.
                if i + 1 < len(markets):
                    next_prefetcher = _make_prefetcher(markets[i + 1]).start()

                reporter.market_started(market.id, market.id)
                replay = OrderBookReplay(current, market_id=market.id, platform=market.platform)
                for event, book in replay:
                    yield market, event, book
                reporter.market_finished(market.id)

                current = next_prefetcher
                next_prefetcher = None
        finally:
            # Generator close mid-iteration: stop any prefetchers we still own.
            # ``current`` is normally cleaned up by OrderBookReplay's iterator
            # finalization, but if we never even started its replay (e.g. early
            # return on empty markets) we still need to shut its thread down.
            if current is not None:
                current.close()
            if next_prefetcher is not None:
                next_prefetcher.close()

    def _make_file_stream(
        self,
        markets: list[Market],
        data_dir: str,
        *,
        after_ms: int | None = None,
        before_ms: int | None = None,
    ) -> Iterator[tuple[Market, HistoryEvent, OrderBook]]:
        """Read market history from local Parquet files instead of the API.

        Applies the same ``[after, before)`` half-open window as the streaming
        path: events with ``t < after_ms`` are silently replayed through
        ``OrderBookReplay`` so the book is fully seeded; events with
        ``t >= before_ms`` halt the per-market stream. The first event yielded
        is therefore guaranteed to come with a book that reflects every prior
        snapshot and delta — matching the streaming endpoint's anchor-then-
        replay semantics.

        Missing parquets stay non-fatal: collector downtime can leave gaps,
        and a backtest should still run on the markets it has data for.
        """
        reporter = self._reporter
        dir_path = Path(data_dir)
        resolved = [(m, self._resolve_history_file(dir_path, m.id)) for m in markets]
        missing = [m for m, p in resolved if p is None]
        if missing:
            warnings.warn(
                f"Skipping {len(missing)} of {len(markets)} markets: "
                f"no history file in {dir_path} (first: {missing[0].id})"
            )
        for market, path in resolved:
            if path is None:
                continue
            events = _iter_history_parquet(path)
            reporter.market_started(market.id, market.id)
            replay = OrderBookReplay(events, market_id=market.id, platform=market.platform)
            for event, book in replay:
                if before_ms is not None and event.t >= before_ms:
                    break
                if after_ms is not None and event.t < after_ms:
                    # Silent replay: book state advances inside ``replay``;
                    # we just don't yield this pre-window event to the engine.
                    continue
                yield market, event, book
            reporter.market_finished(market.id)

    def get_reference_price(self, symbol: str | None, at_time: int) -> str | None:
        if symbol is None:
            return None
        if symbol not in self._ref_prices:
            self._load_reference_prices_for(symbol)
        prices = self._ref_prices.get(symbol)
        if not prices:
            return None
        # Each entry is a candle close at its exact timestamp.
        # Return the most recent close at or before at_time.
        idx = bisect.bisect_right(prices, (at_time, "~")) - 1
        return prices[idx][1] if idx >= 0 else None

    _REF_RESOLUTION_DEFAULT = "1m"
    _REF_RESOLUTION_MS = {
        "1s": 1_000, "5s": 5_000, "10s": 10_000, "30s": 30_000,
        "1m": 60_000, "5m": 300_000, "15m": 900_000,
        "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }

    def _load_reference_prices_for(self, symbol: str) -> None:
        """Synchronously load reference prices for one symbol on first
        request. After this returns ``self._ref_prices[symbol]`` is
        fully populated and subsequent lookups are binary-search cache
        hits. Strategies that never call ``ctx.reference_price()`` skip
        this entirely.
        """
        ctx = self._ref_load_ctx
        data_dir = ctx.get("data_dir")
        if data_dir is not None:
            ref_path = Path(data_dir) / f"reference-{symbol}.parquet"
            if ref_path.exists():
                table = pq.read_table(ref_path, columns=["timestamp", "price"])
                ts_col = table.column("timestamp").to_pylist()
                price_col = [str(v) for v in table.column("price").to_pylist()]
                self._ref_prices[symbol] = list(zip(ts_col, price_col))
                return
        client = ctx.get("client")
        if client is not None:
            # Use the union of the user's window and the registered markets'
            # open/close range — book reconstruction can yield events at the
            # anchor snapshot's timestamp, which may be before `after`.
            bounds = self._underlying_bounds.get(symbol)
            after_in = _coerce_timestamp(ctx.get("after"))
            before_in = _coerce_timestamp(ctx.get("before"))
            if bounds:
                eff_after = bounds[0] if after_in is None else min(after_in, bounds[0])
                eff_before = bounds[1] if before_in is None else max(before_in, bounds[1])
            else:
                eff_after, eff_before = after_in, before_in
            if eff_after is not None and eff_before is not None:
                resolution = ctx.get("resolution") or self._REF_RESOLUTION_DEFAULT
                bucket_ms = self._REF_RESOLUTION_MS.get(resolution, 60_000)
                est_total = max(1, (eff_before - eff_after) // bucket_ms)
                self._reporter.download_started(
                    f"{symbol} reference ({resolution})", est_total,
                )
                prices: list[tuple[int, str]] = []
                for candle in client.reference.candles(
                    symbol, after=eff_after, before=eff_before,
                    resolution=resolution, limit=5000,
                ):
                    prices.append((candle.timestamp, candle.close))
                    self._reporter.download_progress(len(prices))
                self._reporter.download_finished()
                self._ref_prices[symbol] = prices
                return
        raise ValueError(
            f"Cannot load reference prices for {symbol}. "
            f"Use client.exports.download_series() or pass a data_dir."
        )

    def _register_market(self, market: Market) -> None:
        self._market_underlying[market.id] = market.underlying
        if market.underlying and (market.open_time or market.close_time):
            sym = market.underlying
            prev = self._underlying_bounds.get(sym)
            lo = market.open_time or market.close_time
            hi = market.close_time or market.open_time
            if prev is None:
                self._underlying_bounds[sym] = (lo, hi)
            else:
                self._underlying_bounds[sym] = (min(prev[0], lo), max(prev[1], hi))

    def _build_result(self) -> BacktestResult:
        return BacktestResult(
            portfolio=self._portfolio,
            orders=self._orders,
            settlements=self._settlements,
            equity_curve=self._equity_curve,
            cash_rejected=self._cash_rejected,
            config=self._config,
            targets=dict(self._targets),
        )

    def _capture_targets(
        self,
        id: str | list[str],
        *,
        after: Any = None,
        before: Any = None,
        data_dir: str | None = None,
    ) -> None:
        self._targets = {
            "id": id,
            "after": _coerce_timestamp(after),
            "before": _coerce_timestamp(before),
            "data_dir": data_dir,
        }



class BacktestEngine(_EngineCore):
    def run(
        self,
        client: Any,
        id: str | list[str],
        *,
        after: Any = None,
        before: Any = None,
        data_dir: str | None = None,
        reference_resolution: str = "1m",
        **params: Any,
    ) -> BacktestResult:
        self._capture_targets(id, after=after, before=before, data_dir=data_dir)
        # Reference prices are fetched lazily by get_reference_price() on
        # first call — strategies that don't query them pay zero cost.
        # Loaders run on background threads so the engine never blocks.
        self._ref_load_ctx = {
            "client": client, "data_dir": data_dir,
            "after": after, "before": before,
            "resolution": reference_resolution,
        }

        # Pre-coerce the user's window so both stream paths apply identical
        # half-open ``[after, before)`` filtering.
        after_ms = _coerce_timestamp(after)
        before_ms = _coerce_timestamp(before)

        def _stream(markets: list[Market]) -> Iterator[tuple[Market, HistoryEvent, OrderBook]]:
            if data_dir is not None:
                return self._make_file_stream(
                    markets, data_dir, after_ms=after_ms, before_ms=before_ms,
                )
            return self._make_market_stream(client, markets, after=after, before=before)

        replay = data_dir is not None

        if isinstance(id, list):
            _prep_status(f"Resolving {len(id)} target(s)…")
            streams, n_markets, _ = self._resolve_list(
                client, id, after=after, before=before, data_dir=data_dir, **params,
            )
            self._maybe_autodownload(client, id, after=after, before=before, data_dir=data_dir)
            with self._with_reporter(n_markets, replay=replay):
                self._run_merged(streams)
            return self._build_result()

        # 1. Try as a market UUID
        try:
            market = client.markets.get(id)
            self._market_series[market.id] = market.series_id or market.id
            self._register_market(market)
            self._maybe_autodownload(client, id, after=after, before=before, data_dir=data_dir)
            with self._with_reporter(1, replay=replay):
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
                _prep_status(f"Resolving strikes in '{series.title}'…")
                lanes = self._resolve_structured(
                    client, id, series, after=after, before=before, **params,
                )
                n_markets = sum(len(lane) for lane in lanes)
                streams = [_stream(lane) for lane in lanes]
                self._maybe_autodownload(client, id, after=after, before=before, data_dir=data_dir)
                with self._with_reporter(n_markets, replay=replay):
                    self._run_merged(streams)
            elif series.is_rolling:
                _prep_status(f"Resolving markets in '{series.title}'…")
                markets = list(client.series.walk(id, after=after, before=before, **params))
                for m in markets:
                    self._market_series[m.id] = series.id
                    self._market_group[m.id] = series.id
                    self._register_market(m)
                self._maybe_autodownload(client, id, after=after, before=before, data_dir=data_dir)
                with self._with_reporter(len(markets), replay=replay):
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
            self._maybe_autodownload(client, id, after=after, before=before, data_dir=data_dir)
            with self._with_reporter(1, replay=replay):
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
    ) -> tuple[list[Iterator[tuple[Market, HistoryEvent, OrderBook]]], int, list[Market]]:
        after_ms = _coerce_timestamp(after)
        before_ms = _coerce_timestamp(before)

        def _stream(markets: list[Market]) -> Iterator[tuple[Market, HistoryEvent, OrderBook]]:
            if data_dir is not None:
                return self._make_file_stream(
                    markets, data_dir, after_ms=after_ms, before_ms=before_ms,
                )
            return self._make_market_stream(client, markets, after=after, before=before)

        streams: list[Iterator[tuple[Market, HistoryEvent, OrderBook]]] = []
        all_markets: list[Market] = []
        for item_id in ids:
            # Try market UUID
            try:
                market = client.markets.get(item_id)
                self._market_series[market.id] = market.series_id or market.id
                self._register_market(market)
                streams.append(_stream([market]))
                all_markets.append(market)
                continue
            except NotFoundError:
                pass

            # Try series
            series = client.series.get(item_id)
            if series.structured_type:
                lanes = self._resolve_structured(
                    client, item_id, series, after=after, before=before, **params,
                )
                streams.extend(_stream(lane) for lane in lanes)
                for lane in lanes:
                    all_markets.extend(lane)
            elif series.is_rolling:
                markets = list(client.series.walk(item_id, after=after, before=before, **params))
                for m in markets:
                    self._market_series[m.id] = series.id
                    self._market_group[m.id] = series.id
                    self._register_market(m)
                streams.append(_stream(markets))
                all_markets.extend(markets)
            else:
                raise ValueError(
                    f"Series '{series.title}' is neither rolling nor structured."
                )

        return streams, len(all_markets), all_markets

    def _resolve_structured(
        self,
        client: Any,
        series_id: str,
        series: Any,
        *,
        after: Any = None,
        before: Any = None,
        **params: Any,
    ) -> list[list[Market]]:
        """Resolve a structured series into time-disjoint market lanes.

        Each lane is a chain of non-overlapping markets that becomes
        one stream. With overlapping markets (typical for structured
        products), this collapses N markets into K = peak-concurrency
        lanes, bounding the prefetcher count to actual data concurrency.
        """
        event_params = dict(params)
        if after is not None:
            event_params["end_after"] = after
        # Only filter by end_after; many structured events have NULL
        # start_date which causes start_before to exclude them.
        # Individual markets are filtered by open_time/close_time below.
        events = client.series.events(series_id, **event_params).to_list()

        after_ms = _coerce_timestamp(after) if after is not None else None
        before_ms = _coerce_timestamp(before) if before is not None else None

        all_markets: list[Market] = []
        for evt in events:
            # Skip events that end before our window
            if after_ms is not None and evt.end_date and evt.end_date < after_ms:
                continue
            # Skip events that start after our window (when start_date is known)
            if before_ms is not None and evt.start_date and evt.start_date > before_ms:
                continue
            event_markets = client.events.markets(evt.id).to_list()
            for m in event_markets:
                if after_ms is not None and m.close_time and m.close_time < after_ms:
                    continue
                if before_ms is not None and m.open_time and m.open_time > before_ms:
                    continue
                self._market_series[m.id] = series.id
                self._register_market(m)
                all_markets.append(m)

        lanes = _pack_into_lanes(all_markets)
        # Mark all markets in a lane as belonging to the same group so
        # per-lane finalisation in ``_run_merged`` finalises the
        # outgoing market promptly when the next in the lane fires.
        for i, lane in enumerate(lanes):
            lane_key = f"lane:{series.id}:{i}"
            for m in lane:
                self._market_group[m.id] = lane_key
        return lanes


class AsyncBacktestEngine(_EngineCore):
    async def run(
        self,
        client: Any,
        id: str | list[str],
        *,
        after: Any = None,
        before: Any = None,
        data_dir: str | None = None,
        reference_resolution: str = "1m",
        **params: Any,
    ) -> BacktestResult:
        self._capture_targets(id, after=after, before=before, data_dir=data_dir)
        # Async path supports parquet-only reference loading (no API
        # fallback — the sync iterator can't be driven from an async hook).
        # get_reference_price() loads on first call.
        self._ref_load_ctx = {
            "client": None, "data_dir": data_dir,
            "after": after, "before": before,
            "resolution": reference_resolution,
        }

        if isinstance(id, list):
            streams, n_markets = await self._resolve_list(client, id, after=after, before=before, **params)
            with self._with_reporter(n_markets):
                await self._run_merged(streams)
            return self._build_result()

        # 1. Try as a market UUID
        try:
            market = await client.markets.get(id)
            self._market_series[market.id] = market.series_id or market.id
            self._register_market(market)
            with self._with_reporter(1):
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
                lanes = await self._async_resolve_structured(
                    client, id, series, after=after, before=before, **params,
                )
                n_markets = sum(len(lane) for lane in lanes)
                streams = [
                    self._async_make_market_stream(client, lane, after=after, before=before)
                    for lane in lanes
                ]
                with self._with_reporter(n_markets):
                    await self._run_merged(streams)
            elif series.is_rolling:
                markets = []
                async for m in client.series.walk(id, after=after, before=before, **params):
                    markets.append(m)
                for m in markets:
                    self._market_series[m.id] = series.id
                    self._market_group[m.id] = series.id
                    self._register_market(m)
                with self._with_reporter(len(markets)):
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
            with self._with_reporter(1):
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
    ) -> tuple[list[AsyncIterator[tuple[Market, HistoryEvent, OrderBook]]], int]:
        streams: list[AsyncIterator[tuple[Market, HistoryEvent, OrderBook]]] = []
        n_markets = 0
        for item_id in ids:
            # Try market UUID
            try:
                market = await client.markets.get(item_id)
                self._market_series[market.id] = market.series_id or market.id
                self._register_market(market)
                streams.append(self._async_make_market_stream(client, [market], after=after, before=before))
                n_markets += 1
                continue
            except NotFoundError:
                pass

            # Try series
            series = await client.series.get(item_id)
            if series.structured_type:
                lanes = await self._async_resolve_structured(
                    client, item_id, series, after=after, before=before, **params,
                )
                streams.extend(
                    self._async_make_market_stream(client, lane, after=after, before=before)
                    for lane in lanes
                )
                n_markets += sum(len(lane) for lane in lanes)
            elif series.is_rolling:
                markets = []
                async for m in client.series.walk(item_id, after=after, before=before, **params):
                    markets.append(m)
                for m in markets:
                    self._market_series[m.id] = series.id
                    self._market_group[m.id] = series.id
                    self._register_market(m)
                streams.append(self._async_make_market_stream(client, markets, after=after, before=before))
                n_markets += len(markets)
            else:
                raise ValueError(
                    f"Series '{series.title}' is neither rolling nor structured."
                )

        return streams, n_markets

    async def _async_make_market_stream(
        self,
        client: Any,
        markets: list[Market],
        *,
        after: Any = None,
        before: Any = None,
    ) -> AsyncIterator[tuple[Market, HistoryEvent, OrderBook]]:
        """Async version of ``_make_market_stream``.

        Pipelines across market boundaries — see sync version's docstring.
        Per-market query bounds are clamped to ``[open_time, close_time)`` ∩
        the user window for the same reason: streaming and bulk modes must
        see the same per-market events.
        """
        history_params: dict[str, Any] = {}
        if self._config.include_trades:
            history_params["include_trades"] = True
        if self._compact_mode:
            history_params["coalesce"] = True

        reporter = self._reporter
        user_after_ms = _coerce_timestamp(after)
        user_before_ms = _coerce_timestamp(before)

        def _make_prefetcher(market: Market) -> AsyncPrefetchedIterator:
            eff_after = market.open_time if user_after_ms is None else max(
                user_after_ms, market.open_time or user_after_ms,
            )
            eff_before = market.close_time if user_before_ms is None else min(
                user_before_ms, market.close_time or user_before_ms,
            )
            history = client.orderbook.history(
                market.id,
                after=eff_after,
                before=eff_before,
                **history_params,
            )
            mid = market.id
            return AsyncPrefetchedIterator(
                history,
                on_fetched=lambda n, mid=mid: reporter.fetched(mid, n),
                on_done=lambda mid=mid: reporter.market_fetch_done(mid),
            )

        if not markets:
            return

        current = _make_prefetcher(markets[0]).start()
        next_prefetcher: AsyncPrefetchedIterator | None = None
        try:
            for i, market in enumerate(markets):
                if i + 1 < len(markets):
                    next_prefetcher = _make_prefetcher(markets[i + 1]).start()

                reporter.market_started(market.id, market.id)
                replay = AsyncOrderBookReplay(current, market_id=market.id, platform=market.platform)
                async for event, book in replay:
                    yield market, event, book
                reporter.market_finished(market.id)

                current = next_prefetcher
                next_prefetcher = None
        finally:
            if current is not None:
                await current.close()
            if next_prefetcher is not None:
                await next_prefetcher.close()

    async def _run_merged(  # type: ignore[override]
        self,
        streams: list[AsyncIterator[tuple[Market, HistoryEvent, OrderBook]]],
    ) -> None:
        first_book_seen: set[str] = set()
        active: dict[str, Market] = {}

        async for market, event, book in async_merge_streams(streams):
            self._reporter.consumed(market.id, 1)

            # Fire any pending orders scheduled before this event — see the
            # sync ``_run_merged`` for rationale.
            self._drain_pending_before(event.t)

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
    ) -> list[list[Market]]:
        """Resolve a structured series into time-disjoint market lanes.

        See sync ``_resolve_structured`` for the rationale.
        """
        event_params = dict(params)
        if after is not None:
            event_params["end_after"] = after
        if before is not None:
            event_params["start_before"] = before
        events = await (await client.series.events(series_id, **event_params)).to_list()

        after_ms = _coerce_timestamp(after) if after is not None else None
        before_ms = _coerce_timestamp(before) if before is not None else None

        all_markets: list[Market] = []
        for evt in events:
            event_markets = await client.events.markets(evt.id).to_list()
            for m in event_markets:
                if after_ms is not None and m.close_time and m.close_time < after_ms:
                    continue
                if before_ms is not None and m.open_time and m.open_time > before_ms:
                    continue
                self._market_series[m.id] = series.id
                self._register_market(m)
                all_markets.append(m)

        lanes = _pack_into_lanes(all_markets)
        for i, lane in enumerate(lanes):
            lane_key = f"lane:{series.id}:{i}"
            for m in lane:
                self._market_group[m.id] = lane_key
        return lanes
