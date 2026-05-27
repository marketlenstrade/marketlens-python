from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import os
import shutil
import sys
import warnings
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from marketlens.backtest._fees import (
    FeeModel,
    FlatFeeModel,
    PolymarketFeeModel,
    ZeroFeeModel,
)
from marketlens.backtest._types import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SettlementRecord,
)
from marketlens.backtest._portfolio import Portfolio

if TYPE_CHECKING:
    from marketlens.backtest._engine import BacktestConfig

_FOUR = Decimal("0.0001")
_ZERO = Decimal("0")
_FORMAT_VERSION = 1


class BacktestResult:
    def __init__(
        self,
        portfolio: Portfolio,
        orders: list[Order],
        settlements: list[SettlementRecord],
        equity_curve: list[dict],
        cash_rejected: int = 0,
        *,
        config: "BacktestConfig | None" = None,
        targets: dict | None = None,
        market_names: dict[str, str] | None = None,
    ) -> None:
        self._portfolio = portfolio
        self._orders = orders
        self._settlements = settlements
        self._equity_curve = equity_curve
        self._fills = [f for o in orders for f in o.fills]
        self.cash_rejected = cash_rejected
        self.config = config
        self.targets = dict(targets) if targets else {}
        self.market_names: dict[str, str] = dict(market_names) if market_names else {}
        self.initial_cash = portfolio.initial_cash

        initial = Decimal(portfolio.initial_cash)
        final_equity = Decimal(portfolio.equity)

        self.total_pnl = str((final_equity - initial).quantize(_FOUR))
        self.total_return = float((final_equity - initial) / initial) if initial else 0.0
        self.total_trades = len(self._fills)
        self.markets_traded = len({f.market_id for f in self._fills})
        self.total_fees = portfolio.total_fees

        # Fee drag
        total_volume = sum(Decimal(f.price) * Decimal(f.size) for f in self._fills)
        self.fee_drag_bps = (
            float(Decimal(portfolio.total_fees) / total_volume * 10000)
            if total_volume > 0
            else 0.0
        )

        # Win rate & profit factor (net of fees)
        net_pnls = [(s, Decimal(s.pnl) - Decimal(s.fees)) for s in settlements]
        wins = [n for _, n in net_pnls if n > 0]
        losses = [n for _, n in net_pnls if n < 0]
        self.win_rate = len(wins) / len(net_pnls) if net_pnls else 0.0

        gross_profit = sum(wins, _ZERO)
        gross_loss = abs(sum(losses, _ZERO))
        if gross_loss > 0:
            self.profit_factor = float(gross_profit / gross_loss)
        elif gross_profit > 0:
            self.profit_factor = float("inf")
        else:
            self.profit_factor = 0.0

        # Per-settlement returns (shared by Sharpe & Sortino)
        returns: list[float] = []
        if len(settlements) >= 2:
            for s in settlements:
                cb = Decimal(s.avg_entry_price) * Decimal(s.shares)
                if cb > 0:
                    returns.append(float((Decimal(s.pnl) - Decimal(s.fees)) / cb))

        # Sharpe ratio
        self.sharpe_ratio: float | None = None
        if len(returns) >= 2:
            mean_r = sum(returns) / len(returns)
            var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
            std_r = var_r**0.5
            if std_r > 0:
                self.sharpe_ratio = mean_r / std_r

        # Sortino ratio
        self.sortino_ratio: float | None = None
        if len(returns) >= 2:
            neg_returns = [r for r in returns if r < 0]
            if neg_returns:
                mean_r = sum(returns) / len(returns)
                downside_var = sum(r**2 for r in neg_returns) / len(returns)
                downside_dev = downside_var**0.5
                if downside_dev > 0:
                    self.sortino_ratio = mean_r / downside_dev

        # Max drawdown & max drawdown duration
        if equity_curve:
            peak = Decimal(equity_curve[0]["equity"])
            max_dd = _ZERO
            dd_start: int | None = None
            max_dur = 0
            for point in equity_curve:
                eq = Decimal(point["equity"])
                if eq >= peak:
                    if dd_start is not None:
                        dur = point["t"] - dd_start
                        if dur > max_dur:
                            max_dur = dur
                        dd_start = None
                    peak = eq
                else:
                    if dd_start is None:
                        dd_start = point["t"]
                    dd = peak - eq
                    if dd > max_dd:
                        max_dd = dd
            # Handle still in drawdown at end
            if dd_start is not None:
                dur = equity_curve[-1]["t"] - dd_start
                if dur > max_dur:
                    max_dur = dur
            self.max_drawdown = float(max_dd / initial) if initial else 0.0
            self.max_drawdown_duration: int = max_dur
        else:
            self.max_drawdown = 0.0
            self.max_drawdown_duration = 0

        # Avg entry price
        buy_fills = [f for f in self._fills if f.side.value.startswith("BUY")]
        if buy_fills:
            total_cost = sum(Decimal(f.price) * Decimal(f.size) for f in buy_fills)
            total_size = sum(Decimal(f.size) for f in buy_fills)
            self.avg_entry_price = str((total_cost / total_size).quantize(_FOUR))
        else:
            self.avg_entry_price = "0.0000"

        # Expectancy (avg net PnL per settlement)
        if net_pnls:
            total_net = sum(n for _, n in net_pnls)
            self.expectancy = str((total_net / len(net_pnls)).quantize(_FOUR))
        else:
            self.expectancy = "0.0000"

        # Average win / average loss / payoff ratio
        self.avg_win = str((sum(wins) / len(wins)).quantize(_FOUR)) if wins else "0.0000"
        self.avg_loss = str((sum(losses) / len(losses)).quantize(_FOUR)) if losses else "0.0000"
        if losses:
            self.payoff_ratio = (
                float(abs(sum(wins) / len(wins)) / abs(sum(losses) / len(losses)))
                if wins
                else 0.0
            )
        else:
            self.payoff_ratio = float("inf") if wins else 0.0

        # Avg holding period (ms)
        if settlements:
            fill_first: dict[str, int] = {}
            for f in self._fills:
                if f.market_id not in fill_first or f.timestamp < fill_first[f.market_id]:
                    fill_first[f.market_id] = f.timestamp
            durations: list[int] = []
            for s in settlements:
                entry_t = fill_first.get(s.market_id)
                if entry_t is not None:
                    durations.append(s.resolved_at - entry_t)
            self.avg_holding_ms = sum(durations) // len(durations) if durations else 0
        else:
            self.avg_holding_ms = 0

        # Capital utilization
        if equity_curve and initial > 0:
            avg_cash = sum(Decimal(p["cash"]) for p in equity_curve) / len(equity_curve)
            self.capital_utilization = max(0.0, float(1 - avg_cash / initial))
        else:
            self.capital_utilization = 0.0

    def summary(self) -> dict[str, Any]:
        s: dict[str, Any] = {
            "total_pnl": self.total_pnl,
            "total_return": f"{self.total_return:.2%}",
            "win_rate": f"{self.win_rate:.2%}",
            "profit_factor": f"{self.profit_factor:.2f}",
            "max_drawdown": f"{self.max_drawdown:.2%}",
            "sharpe_ratio": (
                f"{self.sharpe_ratio:.2f}" if self.sharpe_ratio is not None else "N/A"
            ),
            "sortino_ratio": (
                f"{self.sortino_ratio:.2f}" if self.sortino_ratio is not None else "N/A"
            ),
            "expectancy": self.expectancy,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "payoff_ratio": f"{self.payoff_ratio:.2f}",
            "avg_holding_ms": self.avg_holding_ms,
            "capital_utilization": f"{self.capital_utilization:.1%}",
            "max_drawdown_duration_ms": self.max_drawdown_duration,
            "total_trades": self.total_trades,
            "markets_traded": self.markets_traded,
            "total_fees": self.total_fees,
            "fee_drag_bps": f"{self.fee_drag_bps:.1f}",
            "avg_entry_price": self.avg_entry_price,
        }
        if self.cash_rejected > 0:
            s["cash_rejected"] = self.cash_rejected
        return s

    def __repr__(self) -> str:
        s = self.summary()
        lines = ["BacktestResult("]
        for k, v in s.items():
            lines.append(f"  {k}={v}")
        lines.append(")")
        return "\n".join(lines)

    def trades_df(self):
        """All fills as a DataFrame."""
        if not self._fills:
            return pd.DataFrame()
        rows = [
            {
                "t": f.timestamp,
                "market_id": f.market_id,
                "side": f.side.value,
                "price": float(f.price),
                "size": float(f.size),
                "fee": float(f.fee),
                "is_maker": f.is_maker,
            }
            for f in self._fills
        ]
        df = pd.DataFrame(rows)
        df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
        return df.set_index("t")

    def orders_df(self):
        """All orders as a DataFrame."""
        if not self._orders:
            return pd.DataFrame()
        rows = [
            {
                "t": o.submitted_at,
                "market_id": o.market_id,
                "side": o.side.value,
                "order_type": o.order_type.value,
                "size": float(o.size),
                "limit_price": float(o.limit_price) if o.limit_price else None,
                "status": o.status.value,
                "filled_size": float(o.filled_size),
                "avg_fill_price": (
                    float(o.avg_fill_price) if o.avg_fill_price else None
                ),
                "total_fees": float(o.total_fees),
            }
            for o in self._orders
        ]
        df = pd.DataFrame(rows)
        df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
        return df.set_index("t")

    def settlements_df(self):
        """Per-market settlement results as a DataFrame."""
        if not self._settlements:
            return pd.DataFrame()
        rows = [
            {
                "market_id": s.market_id,
                "series_id": s.series_id,
                "side": s.side.value,
                "shares": float(s.shares),
                "avg_entry_price": float(s.avg_entry_price),
                "settlement_price": float(s.settlement_price),
                "pnl": float(s.pnl),
                "fees": float(s.fees),
                "winning_outcome": s.winning_outcome,
                "resolved_at": s.resolved_at,
            }
            for s in self._settlements
        ]
        df = pd.DataFrame(rows)
        if "resolved_at" in df.columns:
            df["resolved_at"] = pd.to_datetime(df["resolved_at"], unit="ms", utc=True)
        return df

    def equity_df(self):
        """Equity curve as a DataFrame."""
        if not self._equity_curve:
            return pd.DataFrame()
        df = pd.DataFrame(self._equity_curve)
        df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
        df["cash"] = df["cash"].astype(float)
        df["equity"] = df["equity"].astype(float)
        df["pnl"] = df["pnl"].astype(float)
        return df.set_index("t")

    def by_series(self) -> dict[str | None, dict]:
        """Per-series breakdown of backtest results.

        Returns a dict keyed by ``series_id`` (or ``None`` for unseries'd markets),
        with each value containing aggregated stats for that series.
        """
        from collections import defaultdict

        groups: dict[str | None, list[SettlementRecord]] = defaultdict(list)
        for s in self._settlements:
            groups[s.series_id].append(s)

        result: dict[str | None, dict] = {}
        for sid, settlements in groups.items():
            net_pnls = [(Decimal(s.pnl) - Decimal(s.fees)) for s in settlements]
            total_pnl = sum(net_pnls, _ZERO)
            total_fees = sum(Decimal(s.fees) for s in settlements)
            wins = [n for n in net_pnls if n > 0]
            losses = [n for n in net_pnls if n < 0]
            win_rate = len(wins) / len(net_pnls) if net_pnls else 0.0
            gross_profit = sum(wins, _ZERO)
            gross_loss = abs(sum(losses, _ZERO))
            if gross_loss > 0:
                profit_factor = float(gross_profit / gross_loss)
            elif gross_profit > 0:
                profit_factor = float("inf")
            else:
                profit_factor = 0.0

            market_ids = {s.market_id for s in settlements}
            total_trades = len([
                f for f in self._fills if f.market_id in market_ids
            ])

            expectancy = (
                str((total_pnl / len(net_pnls)).quantize(_FOUR)) if net_pnls else "0.0000"
            )
            avg_win = (
                str((sum(wins, _ZERO) / len(wins)).quantize(_FOUR)) if wins else "0.0000"
            )
            avg_loss = (
                str((sum(losses, _ZERO) / len(losses)).quantize(_FOUR))
                if losses
                else "0.0000"
            )
            if losses:
                payoff_ratio = (
                    float(
                        abs(sum(wins, _ZERO) / len(wins))
                        / abs(sum(losses, _ZERO) / len(losses))
                    )
                    if wins
                    else 0.0
                )
            else:
                payoff_ratio = float("inf") if wins else 0.0

            result[sid] = {
                "total_pnl": str(total_pnl.quantize(_FOUR)),
                "total_fees": str(total_fees.quantize(_FOUR)),
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "expectancy": expectancy,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
                "payoff_ratio": payoff_ratio,
                "markets_traded": len(market_ids),
                "total_trades": total_trades,
            }
        return result

    def to_dataframe(self):
        """Alias for ``settlements_df()`` (SDK convention)."""
        return self.settlements_df()

    # ── Visualization ─────────────────────────────────────────────

    def show(
        self,
        *others: "BacktestResult",
        labels: list[str] | None = None,
        title: str | None = None,
        open_browser: bool = True,
    ) -> None:
        """Open a local browser dashboard for this result.

        Pass additional results as positional args to compare runs side by side.
        The server blocks on the main thread until Ctrl+C.
        """
        from marketlens.backtest._dashboard import show as _show

        _show(self, *others, labels=labels, title=title, open_browser=open_browser)

    @classmethod
    def dashboard(
        cls,
        *paths: str | Path,
        labels: list[str] | None = None,
        title: str | None = None,
        open_browser: bool = True,
    ) -> None:
        """Load saved results and open a browser dashboard."""
        from marketlens.backtest._dashboard import dashboard as _dashboard

        _dashboard(*paths, labels=labels, title=title, open_browser=open_browser)

    # ── Persistence ───────────────────────────────────────────────

    def save(self, path: str | Path, *, overwrite: bool = False) -> Path:
        """Write the result to a directory as parquet + JSON manifest.

        Layout::

            <path>/
              manifest.json
              trades.parquet
              orders.parquet
              settlements.parquet
              equity.parquet

        Reload with :meth:`BacktestResult.load`.
        """
        out = Path(path)
        if out.exists():
            if not overwrite:
                raise FileExistsError(
                    f"{out} already exists. Pass overwrite=True to replace it."
                )
            shutil.rmtree(out)
        out.mkdir(parents=True)

        _write_trades_parquet(out / "trades.parquet", self._fills)
        _write_orders_parquet(out / "orders.parquet", self._orders)
        _write_settlements_parquet(out / "settlements.parquet", self._settlements)
        _write_equity_parquet(out / "equity.parquet", self._equity_curve)

        manifest = {
            "format_version": _FORMAT_VERSION,
            "marketlens_version": _sdk_version(),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "config": _serialize_config(self.config),
            "targets": dict(self.targets),
            "metrics": self._metrics_dict(),
            "cash_rejected": self.cash_rejected,
            "initial_cash": self.initial_cash,
            "market_names": self.market_names,
        }
        with open(out / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)
        _log_status(f"Backtest saved to {out}")
        return out

    @classmethod
    def load(cls, path: str | Path) -> "BacktestResult":
        """Reconstruct a result from a directory previously written by :meth:`save`.

        The returned result is read-only: ``_portfolio`` is ``None`` and the
        backtest cannot be resumed. All metrics and DataFrames are restored
        verbatim.
        """
        src = Path(path)
        with open(src / "manifest.json") as f:
            manifest = json.load(f)

        fmt = manifest.get("format_version")
        if fmt != _FORMAT_VERSION:
            raise ValueError(
                f"Unsupported format_version {fmt!r} (expected {_FORMAT_VERSION}). "
                f"Saved by marketlens=={manifest.get('marketlens_version')!r}."
            )
        saved_v = manifest.get("marketlens_version")
        if saved_v and saved_v != _sdk_version():
            warnings.warn(
                f"Loading backtest saved with marketlens=={saved_v!r}; "
                f"current is {_sdk_version()!r}.",
                stacklevel=2,
            )

        obj = cls.__new__(cls)
        obj._portfolio = None  # type: ignore[assignment]
        obj.cash_rejected = int(manifest.get("cash_rejected", 0))
        obj.initial_cash = manifest.get("initial_cash", "0.0000")
        obj.config = _deserialize_config(manifest.get("config"))
        obj.targets = manifest.get("targets") or {}
        obj.market_names = manifest.get("market_names") or {}

        obj._orders, obj._fills = _read_orders_and_fills(src)
        obj._settlements = _read_settlements(src)
        obj._equity_curve = _read_equity(src)

        for key, val in (manifest.get("metrics") or {}).items():
            if key in ("profit_factor", "payoff_ratio"):
                val = _restore_float(val)
            setattr(obj, key, val)
        return obj

    def _metrics_dict(self) -> dict[str, Any]:
        return {
            "total_pnl": self.total_pnl,
            "total_return": self.total_return,
            "total_trades": self.total_trades,
            "markets_traded": self.markets_traded,
            "total_fees": self.total_fees,
            "fee_drag_bps": self.fee_drag_bps,
            "win_rate": self.win_rate,
            "profit_factor": _json_float(self.profit_factor),
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_duration": self.max_drawdown_duration,
            "avg_entry_price": self.avg_entry_price,
            "expectancy": self.expectancy,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "payoff_ratio": _json_float(self.payoff_ratio),
            "avg_holding_ms": self.avg_holding_ms,
            "capital_utilization": self.capital_utilization,
        }


# ── Persistence helpers ───────────────────────────────────────────


def _sdk_version() -> str:
    try:
        return importlib.metadata.version("marketlens")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _log_status(message: str) -> None:
    """One-line status to stderr; suppressed when MARKETLENS_PROGRESS is falsy."""
    if os.environ.get("MARKETLENS_PROGRESS", "").strip().lower() in {"0", "false", "no", "off"}:
        return
    try:
        sys.stderr.write(f"· {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _json_float(v: float | None) -> float | str | None:
    """Render inf/-inf as strings so the manifest stays strict-JSON."""
    if v is None:
        return None
    if v == float("inf"):
        return "inf"
    if v == float("-inf"):
        return "-inf"
    if v != v:  # NaN
        return None
    return v


def _restore_float(v: Any) -> float | None:
    if v is None:
        return None
    if v == "inf":
        return float("inf")
    if v == "-inf":
        return float("-inf")
    return float(v)


def _serialize_fee_model(model: FeeModel | None) -> dict | None:
    if model is None:
        return None
    if isinstance(model, ZeroFeeModel):
        return {"type": "zero"}
    if isinstance(model, FlatFeeModel):
        return {"type": "flat", "fee_per_share": str(model._fee)}
    if isinstance(model, PolymarketFeeModel):
        return {
            "type": "polymarket",
            "fee_rate": str(model._fee_rate),
            "exponent": model._exponent,
        }
    return None  # custom subclass — falls back to fee_model_repr


def _deserialize_fee_model(data: dict | None) -> FeeModel | None:
    if not data:
        return None
    t = data.get("type")
    if t == "zero":
        return ZeroFeeModel()
    if t == "flat":
        return FlatFeeModel(Decimal(data["fee_per_share"]))
    if t == "polymarket":
        return PolymarketFeeModel(
            Decimal(data["fee_rate"]),
            exponent=int(data.get("exponent", 1)),
        )
    return None


def _serialize_config(config: "BacktestConfig | None") -> dict | None:
    if config is None:
        return None
    out: dict[str, Any] = {}
    for f in dataclasses.fields(config):
        val = getattr(config, f.name)
        if f.name == "fee_model":
            out["fee_model"] = _serialize_fee_model(val)
            out["fee_model_repr"] = repr(val) if val is not None else None
        else:
            out[f.name] = val
    return out


def _deserialize_config(data: dict | None) -> "BacktestConfig | None":
    if not data:
        return None
    from marketlens.backtest._engine import BacktestConfig

    field_names = {f.name for f in dataclasses.fields(BacktestConfig)}
    kwargs: dict[str, Any] = {}
    for k, v in data.items():
        if k == "fee_model":
            kwargs["fee_model"] = _deserialize_fee_model(v)
        elif k == "fee_model_repr":
            continue
        elif k in field_names:
            kwargs[k] = v
    return BacktestConfig(**kwargs)


def _write_trades_parquet(path: Path, fills: list[Fill]) -> None:
    cols = ["order_id", "market_id", "side", "price", "size", "fee", "timestamp", "is_maker"]
    if not fills:
        df = pd.DataFrame({c: [] for c in cols})
    else:
        df = pd.DataFrame([
            {
                "order_id": f.order_id,
                "market_id": f.market_id,
                "side": f.side.value,
                "price": f.price,
                "size": f.size,
                "fee": f.fee,
                "timestamp": f.timestamp,
                "is_maker": f.is_maker,
            }
            for f in fills
        ])
    df.to_parquet(path, index=False)


def _write_orders_parquet(path: Path, orders: list[Order]) -> None:
    cols = [
        "id", "market_id", "side", "order_type", "size", "limit_price",
        "submitted_at", "status", "filled_size", "avg_fill_price",
        "total_fees", "cancel_after",
    ]
    if not orders:
        df = pd.DataFrame({c: [] for c in cols})
    else:
        df = pd.DataFrame([
            {
                "id": o.id,
                "market_id": o.market_id,
                "side": o.side.value,
                "order_type": o.order_type.value,
                "size": o.size,
                "limit_price": o.limit_price,
                "submitted_at": o.submitted_at,
                "status": o.status.value,
                "filled_size": o.filled_size,
                "avg_fill_price": o.avg_fill_price,
                "total_fees": o.total_fees,
                "cancel_after": o.cancel_after,
            }
            for o in orders
        ])
    df.to_parquet(path, index=False)


def _write_settlements_parquet(path: Path, settlements: list[SettlementRecord]) -> None:
    cols = [
        "market_id", "series_id", "side", "shares", "avg_entry_price",
        "settlement_price", "pnl", "fees", "winning_outcome", "resolved_at",
    ]
    if not settlements:
        df = pd.DataFrame({c: [] for c in cols})
    else:
        df = pd.DataFrame([
            {
                "market_id": s.market_id,
                "series_id": s.series_id,
                "side": s.side.value,
                "shares": s.shares,
                "avg_entry_price": s.avg_entry_price,
                "settlement_price": s.settlement_price,
                "pnl": s.pnl,
                "fees": s.fees,
                "winning_outcome": s.winning_outcome,
                "resolved_at": s.resolved_at,
            }
            for s in settlements
        ])
    df.to_parquet(path, index=False)


def _write_equity_parquet(path: Path, equity_curve: list[dict]) -> None:
    cols = ["t", "market_id", "cash", "equity", "pnl"]
    if not equity_curve:
        df = pd.DataFrame({c: [] for c in cols})
    else:
        df = pd.DataFrame(equity_curve, columns=cols)
    df.to_parquet(path, index=False)


def _read_orders_and_fills(src: Path) -> tuple[list[Order], list[Fill]]:
    fills_by_order: dict[str, list[Fill]] = {}
    fills_df = pd.read_parquet(src / "trades.parquet")
    fills: list[Fill] = []
    for _, row in fills_df.iterrows():
        f = Fill(
            order_id=str(row["order_id"]),
            market_id=str(row["market_id"]),
            side=OrderSide(row["side"]),
            price=str(row["price"]),
            size=str(row["size"]),
            fee=str(row["fee"]),
            timestamp=int(row["timestamp"]),
            is_maker=bool(row["is_maker"]),
        )
        fills.append(f)
        fills_by_order.setdefault(f.order_id, []).append(f)

    orders_df = pd.read_parquet(src / "orders.parquet")
    orders: list[Order] = []
    for _, row in orders_df.iterrows():
        oid = str(row["id"])
        orders.append(Order(
            id=oid,
            market_id=str(row["market_id"]),
            side=OrderSide(row["side"]),
            order_type=OrderType(row["order_type"]),
            size=str(row["size"]),
            limit_price=_or_none(row["limit_price"]),
            submitted_at=int(row["submitted_at"]),
            status=OrderStatus(row["status"]),
            filled_size=str(row["filled_size"]),
            avg_fill_price=_or_none(row["avg_fill_price"]),
            total_fees=str(row["total_fees"]),
            fills=fills_by_order.get(oid, []),
            cancel_after=_or_none(row["cancel_after"], cast=int),
        ))
    return orders, fills


def _read_settlements(src: Path) -> list[SettlementRecord]:
    df = pd.read_parquet(src / "settlements.parquet")
    out: list[SettlementRecord] = []
    for _, row in df.iterrows():
        out.append(SettlementRecord(
            market_id=str(row["market_id"]),
            series_id=_or_none(row["series_id"]),
            side=PositionSide(row["side"]),
            shares=str(row["shares"]),
            avg_entry_price=str(row["avg_entry_price"]),
            settlement_price=str(row["settlement_price"]),
            pnl=str(row["pnl"]),
            fees=str(row["fees"]),
            winning_outcome=_or_none(row["winning_outcome"]),
            resolved_at=int(row["resolved_at"]),
        ))
    return out


def _read_equity(src: Path) -> list[dict]:
    df = pd.read_parquet(src / "equity.parquet")
    out: list[dict] = []
    for _, row in df.iterrows():
        out.append({
            "t": int(row["t"]),
            "market_id": str(row["market_id"]),
            "cash": str(row["cash"]),
            "equity": str(row["equity"]),
            "pnl": str(row["pnl"]),
        })
    return out


def _or_none(v: Any, cast: Any = str) -> Any:
    """Pandas/parquet round-trips missing values as NaN; normalize to None
    and cast the rest. Use ``cast=int`` for integer columns.
    """
    if v is None:
        return None
    try:
        if v != v:  # NaN
            return None
    except TypeError:
        pass
    try:
        out = cast(v)
    except (TypeError, ValueError):
        return None
    if cast is str and (not out or out == "None"):
        return None
    return out
