from __future__ import annotations

import bisect
from typing import AsyncIterable, AsyncIterator, Iterable, Iterator

from marketlens.types.history import DeltaEvent, HistoryEvent, SnapshotEvent, TradeEvent
from marketlens.types.orderbook import OrderBook, PriceLevel

_PRICE_DP = 4
_SIZE_DP = 4


def _book_to_row(book: OrderBook) -> dict:
    """Extract standard book metrics into a dict row.

    Empty bid/ask sides are emitted as NaN so DataFrame consumers can
    distinguish a real zero from a missing one.
    """
    has_bid = book.bid_levels > 0
    has_ask = book.ask_levels > 0
    both = has_bid and has_ask
    return {
        "best_bid": book.best_bid if has_bid else None,
        "best_ask": book.best_ask if has_ask else None,
        "spread": book.spread if both else None,
        "midpoint": book.midpoint if both else None,
        "bid_depth": book.bid_depth if has_bid else None,
        "ask_depth": book.ask_depth if has_ask else None,
        "bid_levels": book.bid_levels,
        "ask_levels": book.ask_levels,
        "imbalance": book.imbalance(),
        "weighted_midpoint": book.weighted_midpoint(1),
        "spread_bps": book.spread_bps(),
    }


def _rows_to_dataframe(rows: list[dict]):
    """Convert rows with a ``t`` column (epoch ms) to a DataFrame."""
    import pandas as pd

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("t")
    return df


def _norm_price(price: float) -> float:
    """Quantize a price to 4 decimal places — book keys must collide on
    numerically-equal values regardless of float representation drift."""
    return round(float(price), _PRICE_DP)


class _BookBuilder:
    """Incrementally maintains sorted order book state.

    On snapshot: full rebuild.  On delta: bisect insert/remove of one level.
    Both sides are stored in ascending price order internally.

    Per-event hot path: builds ``OrderBook`` via ``model_construct`` (skipping
    Pydantic validation, since the builder is the sole producer and inputs are
    already canonical) and caches best/spread/midpoint/depth — only refreshing
    the side that changed and only recomputing top-of-book when the best level
    actually moved.
    """

    __slots__ = (
        "_market_id", "_platform",
        "_bid_prices", "_bid_levels", "_bid_depth",
        "_ask_prices", "_ask_levels", "_ask_depth",
        "_best_bid", "_best_ask",
        "_spread", "_midpoint",
    )

    def __init__(self, market_id: str, platform: str) -> None:
        self._market_id = market_id
        self._platform = platform
        self._bid_prices: list[float] = []
        self._bid_levels: list[PriceLevel] = []
        self._bid_depth = 0.0
        self._ask_prices: list[float] = []
        self._ask_levels: list[PriceLevel] = []
        self._ask_depth = 0.0
        self._best_bid: float = 0.0
        self._best_ask: float = 0.0
        self._spread: float = 0.0
        self._midpoint: float = 0.0

    def snapshot(self, bids: list[PriceLevel], asks: list[PriceLevel], as_of: int) -> OrderBook:
        """Full reset from snapshot data."""
        bid_data: dict[float, float] = {}
        for level in bids:
            s = round(level.size, _SIZE_DP)
            if s > 0.0:
                bid_data[_norm_price(level.price)] = s
        self._bid_prices = sorted(bid_data)
        self._bid_levels = [
            PriceLevel.model_construct(price=p, size=bid_data[p])
            for p in self._bid_prices
        ]
        self._bid_depth = round(sum(bid_data.values()), _SIZE_DP)

        ask_data: dict[float, float] = {}
        for level in asks:
            s = round(level.size, _SIZE_DP)
            if s > 0.0:
                ask_data[_norm_price(level.price)] = s
        self._ask_prices = sorted(ask_data)
        self._ask_levels = [
            PriceLevel.model_construct(price=p, size=ask_data[p])
            for p in self._ask_prices
        ]
        self._ask_depth = round(sum(ask_data.values()), _SIZE_DP)

        # Snapshot resets both tops; force spread/midpoint refresh.
        self._best_bid = 0.0
        self._best_ask = 0.0
        self._refresh_top()

        return self._make_book(as_of)

    def delta(self, price: float, size: float, side: str, as_of: int) -> OrderBook:
        """Apply a single price level change and return the full book."""
        self.apply_delta(price, size, side)
        return self._make_book(as_of)

    def apply_delta(self, price: float, size: float, side: str) -> None:
        """Apply a single price level change to internal state (no book build).

        Split out from :meth:`delta` so the hot path can update state without
        materialising an ``OrderBook`` on every delta (see ``_ScalarBook``).
        """
        price = _norm_price(price)
        size = round(size, _SIZE_DP)
        if side == "BUY":
            delta_depth = self._apply(self._bid_prices, self._bid_levels, price, size)
            if delta_depth != 0.0:
                self._bid_depth = round(self._bid_depth + delta_depth, _SIZE_DP)
            new_best = self._bid_prices[-1] if self._bid_prices else 0.0
            if new_best != self._best_bid:
                self._best_bid = new_best
                self._refresh_spread()
        else:
            delta_depth = self._apply(self._ask_prices, self._ask_levels, price, size)
            if delta_depth != 0.0:
                self._ask_depth = round(self._ask_depth + delta_depth, _SIZE_DP)
            new_best = self._ask_prices[0] if self._ask_prices else 0.0
            if new_best != self._best_ask:
                self._best_ask = new_best
                self._refresh_spread()

    @staticmethod
    def _apply(
        prices: list[float], levels: list[PriceLevel], price: float, size: float,
    ) -> float:
        """Insert, update, or remove a single level. Returns depth change."""
        idx = bisect.bisect_left(prices, price)
        exists = idx < len(prices) and prices[idx] == price
        old_size = 0.0

        if exists:
            old_size = levels[idx].size
            if size > 0.0:
                levels[idx] = PriceLevel.model_construct(price=price, size=size)
            else:
                prices.pop(idx)
                levels.pop(idx)
        elif size > 0.0:
            prices.insert(idx, price)
            levels.insert(idx, PriceLevel.model_construct(price=price, size=size))

        return size - old_size

    def _refresh_top(self) -> None:
        """Recompute best_bid/best_ask + spread/midpoint after a snapshot."""
        self._best_bid = self._bid_prices[-1] if self._bid_prices else 0.0
        self._best_ask = self._ask_prices[0] if self._ask_prices else 0.0
        self._refresh_spread()

    def _refresh_spread(self) -> None:
        bb, ba = self._best_bid, self._best_ask
        if bb > 0.0 and ba > 0.0:
            self._spread = round(ba - bb, _PRICE_DP)
            self._midpoint = round((bb + ba) / 2, _PRICE_DP)
        else:
            self._spread = 0.0
            self._midpoint = 0.0

    def _make_book(self, as_of: int) -> OrderBook:
        # model_construct skips validation: this is the sole producer and the
        # inputs were validated when first parsed (or are server-canonical 4dp
        # floats). The list copies keep callers isolated from internal state.
        return OrderBook.model_construct(
            market_id=self._market_id,
            platform=self._platform,
            as_of=as_of,
            bids=self._bid_levels[::-1],
            asks=self._ask_levels[:],
            best_bid=self._best_bid,
            best_ask=self._best_ask,
            spread=self._spread,
            midpoint=self._midpoint,
            bid_depth=self._bid_depth,
            ask_depth=self._ask_depth,
            bid_levels=len(self._bid_levels),
            ask_levels=len(self._ask_levels),
        )


class _ScalarBook:
    """A cheap book view for delta events whose consumer only reads scalars.

    Building a full ``OrderBook`` (pydantic model + bid/ask list copies) on
    every delta dominates replay cost, yet trade-only strategies only read
    scalar fields (best_bid, midpoint, level counts) off delta books — the
    portfolio mark-to-market and cross-market checks never touch the lists.
    This captures the scalar fields eagerly (cheap) and materialises the full
    ``OrderBook`` lazily on first access to ``bids``/``asks`` or any metric
    method.

    Correctness: a builder is only mutated by its own market's events, and the
    engine consumes/replaces each market's book within the same synchronous
    step before that market's next delta — so a lazy materialisation always
    reflects this event's state. (Not safe to retain across the same market's
    later events; backtest consumers don't.)
    """

    __slots__ = (
        "market_id", "platform", "as_of",
        "best_bid", "best_ask", "spread", "midpoint",
        "bid_depth", "ask_depth", "bid_levels", "ask_levels",
        "_builder", "_full",
    )

    def __init__(self, builder: _BookBuilder, as_of: int) -> None:
        self._builder = builder
        self._full: OrderBook | None = None
        self.as_of = as_of
        self.market_id = builder._market_id
        self.platform = builder._platform
        self.best_bid = builder._best_bid
        self.best_ask = builder._best_ask
        self.spread = builder._spread
        self.midpoint = builder._midpoint
        self.bid_depth = builder._bid_depth
        self.ask_depth = builder._ask_depth
        self.bid_levels = len(builder._bid_levels)
        self.ask_levels = len(builder._ask_levels)

    def materialize(self) -> OrderBook:
        if self._full is None:
            self._full = self._builder._make_book(self.as_of)
        return self._full

    @property
    def bids(self) -> list[PriceLevel]:
        return self.materialize().bids

    @property
    def asks(self) -> list[PriceLevel]:
        return self.materialize().asks

    def impact(self, side: str, size: float) -> float | None:
        return self.materialize().impact(side, size)

    def depth_within(self, spread: float) -> tuple[float, float]:
        return self.materialize().depth_within(spread)

    def slippage(self, side: str, size: float) -> float | None:
        return self.materialize().slippage(side, size)

    def microprice(self) -> float | None:
        return self.materialize().microprice()

    def spread_bps(self) -> float | None:
        return self.materialize().spread_bps()

    def imbalance(self, levels: int | None = None) -> float | None:
        return self.materialize().imbalance(levels)

    def weighted_midpoint(self, n: int = 1) -> float | None:
        return self.materialize().weighted_midpoint(n)


class OrderBookReplay:
    """Reconstruct full orderbook state from a history event stream.

    Yields ``(event, book)`` tuples where ``book`` is the full ``OrderBook``
    state after applying the event.

    Usage::

        history = client.orderbook.history(market_id, after=start, before=end)
        for event, book in OrderBookReplay(history, market_id=market_id):
            print(f"t={event.t}  spread={book.spread}")

    Args:
        events: Iterable of history events (from ``client.orderbook.history()``).
        market_id: Market identifier (used in the resulting OrderBook objects).
        platform: Platform name (defaults to ``"polymarket"``).
    """

    def __init__(
        self,
        events: Iterable[HistoryEvent],
        market_id: str = "",
        platform: str = "polymarket",
        lazy_deltas: bool = False,
    ) -> None:
        self._events = events
        self._market_id = market_id
        self._platform = platform
        # When True, delta events yield a cheap _ScalarBook that builds the full
        # OrderBook lazily. Safe only when the consumer reads delta books as
        # transient scalar views (e.g. a trade-only backtest strategy).
        self._lazy_deltas = lazy_deltas

    def __iter__(self) -> Iterator[tuple[HistoryEvent, OrderBook]]:
        builder = _BookBuilder(self._market_id, self._platform)
        book: OrderBook | _ScalarBook | None = None
        initialized = False

        for event in self._events:
            if isinstance(event, SnapshotEvent):
                book = builder.snapshot(event.bids, event.asks, event.t)
                initialized = True
                yield event, book

            elif isinstance(event, DeltaEvent):
                if not initialized:
                    raise ValueError(
                        "OrderBookReplay received a delta before any snapshot. "
                        "The history stream must begin with a snapshot event."
                    )
                if self._lazy_deltas:
                    builder.apply_delta(event.price, event.size, event.side)
                    book = _ScalarBook(builder, event.t)
                else:
                    book = builder.delta(event.price, event.size, event.side, event.t)
                yield event, book

            elif isinstance(event, TradeEvent):
                if not initialized:
                    raise ValueError(
                        "OrderBookReplay received a trade before any snapshot. "
                        "The history stream must begin with a snapshot event."
                    )
                # Hand on_trade a full book (cheap: only at trades, not deltas).
                if isinstance(book, _ScalarBook):
                    book = book.materialize()
                yield event, book

    def to_dataframe(self):
        """Replay the event stream and return a DataFrame of book state over time.

        Each row corresponds to one event. Columns include:

        - ``t`` — event timestamp (``datetime64[ns, UTC]``)
        - ``event_type`` — ``"snapshot"``, ``"delta"``, or ``"trade"``
        - ``best_bid``, ``best_ask``, ``spread``, ``midpoint`` — ``float64``
        - ``bid_depth``, ``ask_depth`` — ``float64``
        - ``bid_levels``, ``ask_levels`` — ``int``
        - ``imbalance`` — ``float64`` (bid-ask imbalance in ``[-1, 1]``)
        - ``weighted_midpoint`` — ``float64`` (top-of-book size-weighted mid)
        - ``spread_bps`` — ``float64`` (spread in basis points)

        """
        rows: list[dict] = []
        for event, book in self:
            row = _book_to_row(book)
            row["t"] = event.t
            row["event_type"] = event.type

            if isinstance(event, TradeEvent):
                row["trade_price"] = event.price
                row["trade_size"] = event.size
                row["trade_side"] = event.side

            rows.append(row)

        return _rows_to_dataframe(rows)


class AsyncOrderBookReplay:
    """Async version of OrderBookReplay for use with AsyncPageIterator."""

    def __init__(
        self,
        events: AsyncIterable[HistoryEvent],
        market_id: str = "",
        platform: str = "polymarket",
    ) -> None:
        self._events = events
        self._market_id = market_id
        self._platform = platform

    async def __aiter__(self) -> AsyncIterator[tuple[HistoryEvent, OrderBook]]:
        builder = _BookBuilder(self._market_id, self._platform)
        book: OrderBook | None = None
        initialized = False

        async for event in self._events:
            if isinstance(event, SnapshotEvent):
                book = builder.snapshot(event.bids, event.asks, event.t)
                initialized = True
                yield event, book

            elif isinstance(event, DeltaEvent):
                if not initialized:
                    raise ValueError(
                        "OrderBookReplay received a delta before any snapshot. "
                        "The history stream must begin with a snapshot event."
                    )
                book = builder.delta(event.price, event.size, event.side, event.t)
                yield event, book

            elif isinstance(event, TradeEvent):
                if not initialized:
                    raise ValueError(
                        "OrderBookReplay received a trade before any snapshot. "
                        "The history stream must begin with a snapshot event."
                    )
                yield event, book

    async def to_dataframe(self):
        """Async version of :meth:`OrderBookReplay.to_dataframe`."""
        rows: list[dict] = []
        async for event, book in self:
            row = _book_to_row(book)
            row["t"] = event.t
            row["event_type"] = event.type

            if isinstance(event, TradeEvent):
                row["trade_price"] = event.price
                row["trade_size"] = event.size
                row["trade_side"] = event.side

            rows.append(row)

        return _rows_to_dataframe(rows)
