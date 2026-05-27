from __future__ import annotations

import json
import threading
import time
from decimal import Decimal

import httpx
import pytest

from marketlens.backtest._dashboard._serialize import (
    _compute_drawdown,
    _safe_float,
    serialize_results,
)
from marketlens.backtest._dashboard._server import _DashboardHandler, serve
from marketlens.backtest._portfolio import Portfolio
from marketlens.backtest._results import BacktestResult
from marketlens.backtest._types import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SettlementRecord,
)


def _make_result(
    pnl: str = "35.0000",
    initial_cash: str = "10000.0000",
    n_markets: int = 1,
) -> BacktestResult:
    """Build a minimal BacktestResult for dashboard testing."""
    portfolio = Portfolio(initial_cash)
    orders = []
    settlements = []
    fills_for_portfolio = []

    for i in range(n_markets):
        mid = f"market-{i}"
        fill = Fill(
            order_id=f"o{i}",
            market_id=mid,
            side=OrderSide.BUY_YES,
            price="0.6500",
            size="100.0000",
            fee="1.0000",
            timestamp=1000 + i * 1000,
            is_maker=False,
        )
        order = Order(
            id=f"o{i}",
            market_id=mid,
            side=OrderSide.BUY_YES,
            order_type=OrderType.MARKET,
            size="100.0000",
            submitted_at=1000 + i * 1000,
            status=OrderStatus.FILLED,
            filled_size="100.0000",
            avg_fill_price="0.6500",
            total_fees="1.0000",
            fills=[fill],
        )
        settlement = SettlementRecord(
            market_id=mid,
            series_id=None,
            side=PositionSide.YES,
            shares="100.0000",
            avg_entry_price="0.6500",
            settlement_price="1.0000",
            pnl=pnl,
            fees="1.0000",
            winning_outcome="Yes",
            resolved_at=6000 + i * 1000,
        )
        orders.append(order)
        settlements.append(settlement)
        fills_for_portfolio.append(fill)

    for fill in fills_for_portfolio:
        portfolio.apply_fill(fill)

    equity_curve = [
        {"t": 1000, "market_id": "market-0", "cash": "9934.0000", "equity": "9934.0000", "pnl": "-66.0000"},
        {"t": 3000, "market_id": "market-0", "cash": "9950.0000", "equity": "9950.0000", "pnl": "-50.0000"},
        {"t": 6000, "market_id": "market-0", "cash": "10034.0000", "equity": "10034.0000", "pnl": "34.0000"},
    ]

    return BacktestResult(
        portfolio=portfolio,
        orders=orders,
        settlements=settlements,
        equity_curve=equity_curve,
        cash_rejected=0,
    )


# ── Serialization tests ──────────────────────────────────────


class TestSerialize:
    def test_single_result(self):
        result = _make_result()
        data = serialize_results([result])
        assert len(data["runs"]) == 1
        run = data["runs"][0]
        assert run["label"] == "Run 1"
        assert run["metrics"]["total_trades"] == 1
        assert run["metrics"]["markets_traded"] == 1
        assert isinstance(run["metrics"]["total_pnl"], float)
        assert len(run["equity_curve"]) == 3
        assert len(run["trades"]) == 1
        assert len(run["settlements"]) == 1

    def test_comparison(self):
        r1 = _make_result()
        r2 = _make_result(pnl="50.0000", n_markets=2)
        data = serialize_results([r1, r2], labels=["Baseline", "Improved"])
        assert len(data["runs"]) == 2
        assert data["runs"][0]["label"] == "Baseline"
        assert data["runs"][1]["label"] == "Improved"
        assert data["runs"][1]["metrics"]["markets_traded"] == 2

    def test_empty_result(self):
        portfolio = Portfolio("10000.0000")
        result = BacktestResult(
            portfolio=portfolio,
            orders=[],
            settlements=[],
            equity_curve=[],
        )
        data = serialize_results([result])
        run = data["runs"][0]
        assert run["metrics"]["total_trades"] == 0
        assert run["equity_curve"] == []
        assert run["drawdown_curve"] == []
        assert run["trades"] == []
        assert run["settlements"] == []
        assert run["pnl_by_market"] == []

    def test_safe_float_inf(self):
        assert _safe_float(float("inf")) is None
        assert _safe_float(float("-inf")) is None
        assert _safe_float(float("nan")) is None
        assert _safe_float(None) is None
        assert _safe_float("0.6500") == 0.65
        assert _safe_float(42) == 42.0

    def test_drawdown_computation(self):
        curve = [
            {"t": 1, "equity": "100", "cash": "100", "pnl": "0"},
            {"t": 2, "equity": "110", "cash": "110", "pnl": "10"},
            {"t": 3, "equity": "99", "cash": "99", "pnl": "-1"},
            {"t": 4, "equity": "120", "cash": "120", "pnl": "20"},
        ]
        dd = _compute_drawdown(curve)
        assert len(dd) == 4
        assert dd[0]["drawdown"] == 0.0
        assert dd[1]["drawdown"] == 0.0
        assert dd[2]["drawdown"] == pytest.approx(-11 / 110, rel=1e-6)
        assert dd[3]["drawdown"] == 0.0

    def test_pnl_by_market_aggregated(self):
        result = _make_result(n_markets=3)
        data = serialize_results([result])
        pnl_markets = data["runs"][0]["pnl_by_market"]
        assert len(pnl_markets) == 3
        assert all("market_id" in m and "net_pnl" in m for m in pnl_markets)

    def test_order_stats(self):
        result = _make_result(n_markets=2)
        data = serialize_results([result])
        stats = data["runs"][0]["order_stats"]
        assert stats["total"] == 2
        assert "FILLED" in stats["by_status"]
        assert "MARKET" in stats["by_type"]
        assert 0 <= stats["fill_rate"] <= 1
        assert 0 <= stats["maker_pct"] <= 1

    def test_settlements_have_net_pnl(self):
        result = _make_result()
        data = serialize_results([result])
        s = data["runs"][0]["settlements"][0]
        assert "net_pnl" in s
        assert s["net_pnl"] == s["pnl"] - s["fees"]

    def test_equity_downsampled(self):
        portfolio = Portfolio("10000.0000")
        big_curve = [
            {"t": i, "market_id": "m", "cash": "10000", "equity": str(10000 + i), "pnl": str(i)}
            for i in range(5000)
        ]
        result = BacktestResult(
            portfolio=portfolio, orders=[], settlements=[], equity_curve=big_curve,
        )
        data = serialize_results([result])
        assert len(data["runs"][0]["equity_curve"]) <= 2000


# ── Server tests ─────────────────────────────────────────────


class TestServer:
    def test_serves_data_and_static(self):
        import socketserver

        result = _make_result()
        data = serialize_results([result])
        import orjson
        data_bytes = orjson.dumps(data)

        handler = type("_H", (_DashboardHandler,), {"_data_bytes": data_bytes})

        with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()

            try:
                base = f"http://127.0.0.1:{port}"

                resp = httpx.get(f"{base}/api/data")
                assert resp.status_code == 200
                body = resp.json()
                assert "runs" in body
                assert len(body["runs"]) == 1

                resp = httpx.get(f"{base}/")
                assert resp.status_code == 200
                assert "Backtest Dashboard" in resp.text

                resp = httpx.get(f"{base}/style.css")
                assert resp.status_code == 200
                assert "var(--bg)" in resp.text

                resp = httpx.get(f"{base}/dashboard.js")
                assert resp.status_code == 200

                resp = httpx.get(f"{base}/nonexistent.xyz")
                assert resp.status_code == 404
            finally:
                httpd.shutdown()

    def test_path_traversal_blocked(self):
        import socketserver

        handler = type("_H", (_DashboardHandler,), {"_data_bytes": b"{}"})
        with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{port}"
                resp = httpx.get(f"{base}/../../../etc/passwd")
                assert resp.status_code == 404
            finally:
                httpd.shutdown()


# ── Integration tests ────────────────────────────────────────


class TestIntegration:
    def test_show_method_exists(self):
        assert hasattr(BacktestResult, "show")
        assert callable(BacktestResult.show)

    def test_dashboard_classmethod_exists(self):
        assert hasattr(BacktestResult, "dashboard")
        assert callable(BacktestResult.dashboard)

    def test_show_no_results_raises(self):
        from marketlens.backtest._dashboard import show

        with pytest.raises(ValueError, match="At least one"):
            show()

    def test_labels_mismatch_raises(self):
        from marketlens.backtest._dashboard import show

        result = _make_result()
        with pytest.raises(ValueError, match="labels length"):
            show(result, labels=["A", "B"])

    def test_dashboard_no_paths_raises(self):
        from marketlens.backtest._dashboard import dashboard

        with pytest.raises(ValueError, match="At least one"):
            dashboard()
