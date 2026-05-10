from __future__ import annotations

import bisect
from decimal import Decimal
from typing import AsyncIterable, AsyncIterator, Iterable, Iterator

from marketlens.types.history import DeltaEvent, HistoryEvent, SnapshotEvent, TradeEvent
from marketlens.types.orderbook import OrderBook, PriceLevel

FOUR = Decimal("0.0001")
ZERO = Decimal("0")


def _book_to_row(book: OrderBook) -> dict:
    """Extract standard book metrics into a dict row."""
    wmid = book.weighted_midpoint(1)
    return {
        "best_bid": float(book.best_bid) if book.best_bid else None,
        "best_ask": float(book.best_ask) if book.best_ask else None,
        "spread": float(book.spread) if book.spread else None,
        "midpoint": float(book.midpoint) if book.midpoint else None,
        "bid_depth": float(book.bid_depth) if book.bid_depth else None,
        "ask_depth": float(book.ask_depth) if book.ask_depth else None,
        "bid_levels": book.bid_levels,
        "ask_levels": book.ask_levels,
        "imbalance": book.imbalance(),
        "weighted_midpoint": float(wmid) if wmid else None,
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


def _norm_price(price: str) -> str:
    """Normalize a price string to 4 decimal places."""
    return str(Decimal(price).quantize(FOUR))


class _BookBuilder:
    """Incrementally maintains sorted order book state.

    On snapshot: full rebuild.  On delta: bisect insert/remove of one level.
    Both sides are stored in ascending price order internally.

    Per-event hot path: builds ``OrderBook`` via ``model_construct`` (skipping
    Pydantic validation, since the builder is the sole producer and inputs are
    already canonical) and caches best/spread/midpoint/depth strings — only
    refreshing the side that changed and only recomputing top-of-book when
    the best level actually moved.
    """

    __slots__ = (
        "_market_id", "_platform",
        "_bid_prices", "_bid_levels", "_bid_depth",
        "_ask_prices", "_ask_levels", "_ask_depth",
        "_best_bid", "_best_ask",
        "_spread", "_midpoint",
        "_bid_depth_str", "_ask_depth_str",
    )

    def __init__(self, market_id: str, platform: str) -> None:
        self._market_id = market_id
        self._platform = platform
        self._bid_prices: list[str] = []
        self._bid_levels: list[PriceLevel] = []
        self._bid_depth = ZERO
        self._ask_prices: list[str] = []
        self._ask_levels: list[PriceLevel] = []
        self._ask_depth = ZERO
        self._best_bid: str | None = None
        self._best_ask: str | None = None
        self._spread: str | None = None
        self._midpoint: str | None = None
        self._bid_depth_str = "0.0000"
        self._ask_depth_str = "0.0000"

    def snapshot(self, bids: list[PriceLevel], asks: list[PriceLevel], as_of: int) -> OrderBook:
        """Full reset from snapshot data."""
        bid_data: dict[str, Decimal] = {}
        for level in bids:
            s = Decimal(level.size)
            if s > ZERO:
                bid_data[_norm_price(level.price)] = s
        self._bid_prices = sorted(bid_data)
        self._bid_levels = [
            PriceLevel.model_construct(price=p, size=str(bid_data[p].quantize(FOUR)))
            for p in self._bid_prices
        ]
        self._bid_depth = sum(bid_data.values(), ZERO)
        self._bid_depth_str = str(self._bid_depth.quantize(FOUR))

        ask_data: dict[str, Decimal] = {}
        for level in asks:
            s = Decimal(level.size)
            if s > ZERO:
                ask_data[_norm_price(level.price)] = s
        self._ask_prices = sorted(ask_data)
        self._ask_levels = [
            PriceLevel.model_construct(price=p, size=str(ask_data[p].quantize(FOUR)))
            for p in self._ask_prices
        ]
        self._ask_depth = sum(ask_data.values(), ZERO)
        self._ask_depth_str = str(self._ask_depth.quantize(FOUR))

        # Snapshot resets both tops; force spread/midpoint refresh.
        self._best_bid = None
        self._best_ask = None
        self._refresh_top()

        return self._make_book(as_of)

    def delta(self, price: str, size: Decimal, side: str, as_of: int) -> OrderBook:
        """Apply a single price level change."""
        price = _norm_price(price)
        if side == "BUY":
            delta_depth = self._apply(self._bid_prices, self._bid_levels, price, size)
            if delta_depth != ZERO:
                self._bid_depth += delta_depth
                self._bid_depth_str = str(self._bid_depth.quantize(FOUR))
            new_best = self._bid_prices[-1] if self._bid_prices else None
            if new_best != self._best_bid:
                self._best_bid = new_best
                self._refresh_spread()
        else:
            delta_depth = self._apply(self._ask_prices, self._ask_levels, price, size)
            if delta_depth != ZERO:
                self._ask_depth += delta_depth
                self._ask_depth_str = str(self._ask_depth.quantize(FOUR))
            new_best = self._ask_prices[0] if self._ask_prices else None
            if new_best != self._best_ask:
                self._best_ask = new_best
                self._refresh_spread()
        return self._make_book(as_of)

    @staticmethod
    def _apply(
        prices: list[str], levels: list[PriceLevel], price: str, size: Decimal,
    ) -> Decimal:
        """Insert, update, or remove a single level. Returns depth change."""
        idx = bisect.bisect_left(prices, price)
        exists = idx < len(prices) and prices[idx] == price
        old_size = ZERO

        if exists:
            old_size = Decimal(levels[idx].size)
            if size > ZERO:
                levels[idx] = PriceLevel.model_construct(
                    price=price, size=str(size.quantize(FOUR)),
                )
            else:
                prices.pop(idx)
                levels.pop(idx)
        elif size > ZERO:
            prices.insert(idx, price)
            levels.insert(idx, PriceLevel.model_construct(
                price=price, size=str(size.quantize(FOUR)),
            ))

        return size - old_size

    def _refresh_top(self) -> None:
        """Recompute best_bid/best_ask + spread/midpoint after a snapshot."""
        self._best_bid = self._bid_prices[-1] if self._bid_prices else None
        self._best_ask = self._ask_prices[0] if self._ask_prices else None
        self._refresh_spread()

    def _refresh_spread(self) -> None:
        bb, ba = self._best_bid, self._best_ask
        if bb is not None and ba is not None:
            bb_d = Decimal(bb)
            ba_d = Decimal(ba)
            self._spread = str((ba_d - bb_d).quantize(FOUR))
            self._midpoint = str(((bb_d + ba_d) / 2).quantize(FOUR))
        else:
            self._spread = None
            self._midpoint = None

    def _make_book(self, as_of: int) -> OrderBook:
        # model_construct skips validation: this is the sole producer and the
        # inputs were validated when first parsed (or are server-canonical 4dp
        # strings). The list copies keep callers isolated from internal state.
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
            bid_depth=self._bid_depth_str,
            ask_depth=self._ask_depth_str,
            bid_levels=len(self._bid_levels),
            ask_levels=len(self._ask_levels),
        )


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
    ) -> None:
        self._events = events
        self._market_id = market_id
        self._platform = platform

    def __iter__(self) -> Iterator[tuple[HistoryEvent, OrderBook]]:
        builder = _BookBuilder(self._market_id, self._platform)
        book: OrderBook | None = None
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
                book = builder.delta(event.price, Decimal(event.size), event.side, event.t)
                yield event, book

            elif isinstance(event, TradeEvent):
                if not initialized:
                    raise ValueError(
                        "OrderBookReplay received a trade before any snapshot. "
                        "The history stream must begin with a snapshot event."
                    )
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
                row["trade_price"] = float(event.price)
                row["trade_size"] = float(event.size)
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
                book = builder.delta(event.price, Decimal(event.size), event.side, event.t)
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
                row["trade_price"] = float(event.price)
                row["trade_size"] = float(event.size)
                row["trade_side"] = event.side

            rows.append(row)

        return _rows_to_dataframe(rows)
