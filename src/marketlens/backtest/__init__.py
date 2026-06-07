"""MarketLens backtesting engine for prediction market strategies."""

from marketlens.backtest._engine import (
    AsyncBacktestEngine,
    BacktestConfig,
    BacktestEngine,
    run_strategies,
)
from marketlens.backtest._fees import FeeModel, FlatFeeModel, PolymarketFeeModel, ZeroFeeModel
from marketlens.backtest._results import BacktestResult, MultiBacktestResult
from marketlens.backtest._strategy import Strategy, StrategyContext
from marketlens.backtest._types import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    SettlementRecord,
)

__all__ = [
    "AsyncBacktestEngine",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "FeeModel",
    "Fill",
    "FlatFeeModel",
    "MultiBacktestResult",
    "Order",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PolymarketFeeModel",
    "Position",
    "PositionSide",
    "SettlementRecord",
    "Strategy",
    "StrategyContext",
    "ZeroFeeModel",
    "run_strategies",
]
