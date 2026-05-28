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
        {"name": "Yes", "index": 0, "platform_token_id": "tok1", "last_price": 0.65},
        {"name": "No", "index": 1, "platform_token_id": "tok2", "last_price": 0.35},
    ],
    "winning_outcome": None,
    "winning_outcome_index": None,
    "tick_size": 0.01,
    "volume": 50000.0,
    "liquidity": 10000.0,
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
    "price": 0.65,
    "size": 150.0,
    "side": "BUY",
    "platform_timestamp": 1700000001000,
    "collected_at": 1700000001050,
    "fee_rate_bps": 50.0,
}

SAMPLE_CANDLE = {
    "open_time": 1700000000000,
    "close_time": 1700003599999,
    "open": 0.64,
    "high": 0.68,
    "low": 0.63,
    "close": 0.66,
    "vwap": 0.6537,
    "volume": 12500.0,
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
        {"price": 0.65, "size": 200.0},
        {"price": 0.64, "size": 150.0},
        {"price": 0.63, "size": 500.0},
    ],
    "asks": [
        {"price": 0.67, "size": 100.0},
        {"price": 0.68, "size": 250.0},
        {"price": 0.69, "size": 400.0},
    ],
    "best_bid": 0.65,
    "best_ask": 0.67,
    "spread": 0.02,
    "midpoint": 0.66,
    "bid_depth": 850.0,
    "ask_depth": 750.0,
    "bid_levels": 3,
    "ask_levels": 3,
}

SAMPLE_BOOK_METRICS = {
    "t": 1700000100000,
    "best_bid": 0.65,
    "best_ask": 0.67,
    "spread": 0.02,
    "midpoint": 0.66,
    "bid_depth": 850.0,
    "ask_depth": 750.0,
    "bid_levels": 18,
    "ask_levels": 25,
}
