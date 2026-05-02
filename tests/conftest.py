import os

# Disable progress bars for the entire test suite so existing tests don't
# accidentally exercise rich's Live renderer in non-TTY pytest output.
os.environ.setdefault("MARKETLENS_PROGRESS", "0")

import pytest
import respx
import httpx

from marketlens import MarketLens

BASE_URL = "https://api.marketlens.com/v1"


@pytest.fixture
def mock_api():
    with respx.mock(base_url=BASE_URL) as respx_mock:
        yield respx_mock


@pytest.fixture
def client():
    c = MarketLens(api_key="mk_test_key", base_url=BASE_URL)
    yield c
    c.close()


# ── Sample response fixtures ──────────────────────────────────

SAMPLE_MARKET = {
    "id": "abc-123",
    "platform": "polymarket",
    "platform_market_id": "0xabc",
    "event_id": "evt-1",
    "event_title": "Test Event",
    "category": "Crypto",
    "series_id": None,
    "series_title": None,
    "series_recurrence": None,
    "question": "Will BTC reach 100k?",
    "market_type": "binary",
    "status": "active",
    "outcomes": [
        {"name": "Yes", "index": 0, "platform_token_id": "tok1", "last_price": "0.6500"},
        {"name": "No", "index": 1, "platform_token_id": "tok2", "last_price": "0.3500"},
    ],
    "winning_outcome": None,
    "winning_outcome_index": None,
    "tick_size": "0.0100",
    "volume": "50000.0000",
    "liquidity": "10000.0000",
    "open_time": 1700000000000,
    "close_time": 1709000000000,
    "resolved_at": None,
    "platform_resolved_at": None,
    "created_at": 1699900000000,
    "updated_at": 1700000000000,
}

SAMPLE_TRADE = {
    "id": "01ABC123",
    "market_id": "abc-123",
    "platform": "polymarket",
    "price": "0.6500",
    "size": "150.0000",
    "side": "BUY",
    "platform_timestamp": 1700000001000,
    "collected_at": 1700000001050,
    "fee_rate_bps": "50",
}

SAMPLE_CANDLE = {
    "open_time": 1700000000000,
    "close_time": 1700003599999,
    "open": "0.6400",
    "high": "0.6800",
    "low": "0.6300",
    "close": "0.6600",
    "vwap": "0.6537",
    "volume": "12500.0000",
    "trade_count": 47,
}

SAMPLE_EVENT = {
    "id": "evt-1",
    "platform": "polymarket",
    "platform_event_id": "evt_abc",
    "title": "Test Event",
    "category": "Crypto",
    "series_id": None,
    "series_title": None,
    "series_recurrence": None,
    "market_count": 3,
    "start_date": 1700000000000,
    "end_date": 1709000000000,
    "created_at": 1699900000000,
    "updated_at": 1700000000000,
}

SAMPLE_SERIES = {
    "id": "btc-daily",
    "platform": "polymarket",
    "platform_series_id": "btc-up-or-down-daily",
    "title": "BTC Up or Down Daily",
    "recurrence": "daily",
    "category": "Crypto",
    "is_rolling": True,
    "market_count": 365,
    "first_market_close": 1640000000000,
    "last_market_close": 1709000000000,
}

SAMPLE_SERIES_NONROLLING = {
    "id": "btc-hit-price",
    "platform": "polymarket",
    "platform_series_id": "bitcoin-hit-price-weekly",
    "title": "Bitcoin Hit Price Weekly",
    "recurrence": "weekly",
    "category": "Crypto",
    "is_rolling": False,
    "market_count": 52,
    "first_market_close": 1640000000000,
    "last_market_close": 1709000000000,
}

SAMPLE_ORDERBOOK = {
    "market_id": "abc-123",
    "platform": "polymarket",
    "as_of": 1700000000047,
    "bids": [
        {"price": "0.6500", "size": "200.0000"},
        {"price": "0.6400", "size": "150.0000"},
        {"price": "0.6300", "size": "500.0000"},
    ],
    "asks": [
        {"price": "0.6700", "size": "100.0000"},
        {"price": "0.6800", "size": "250.0000"},
        {"price": "0.6900", "size": "400.0000"},
    ],
    "best_bid": "0.6500",
    "best_ask": "0.6700",
    "spread": "0.0200",
    "midpoint": "0.6600",
    "bid_depth": "850.0000",
    "ask_depth": "750.0000",
    "bid_levels": 3,
    "ask_levels": 3,
}

SAMPLE_BOOK_METRICS = {
    "t": 1700000100000,
    "best_bid": "0.6500",
    "best_ask": "0.6700",
    "spread": "0.0200",
    "midpoint": "0.6600",
    "bid_depth": "850.0000",
    "ask_depth": "750.0000",
    "bid_levels": 18,
    "ask_levels": 25,
}
