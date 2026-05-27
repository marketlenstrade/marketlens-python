"""Backtest dashboard — local browser visualization."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from marketlens.backtest._results import BacktestResult


def show(
    *results: BacktestResult,
    labels: list[str] | None = None,
    title: str | None = None,
    open_browser: bool = True,
) -> None:
    """Open a local browser dashboard for one or more backtest results.

    The server blocks until Ctrl+C.
    """
    from marketlens.backtest._dashboard._serialize import serialize_results
    from marketlens.backtest._dashboard._server import serve

    if not results:
        raise ValueError("At least one BacktestResult is required.")
    if labels and len(labels) != len(results):
        raise ValueError(
            f"labels length ({len(labels)}) must match results length ({len(results)})."
        )

    data = serialize_results(list(results), labels=labels, title=title)
    serve(data, open_browser=open_browser)


def dashboard(
    *paths: str | Path,
    labels: list[str] | None = None,
    title: str | None = None,
    open_browser: bool = True,
) -> None:
    """Load saved backtest results and open a browser dashboard."""
    from marketlens.backtest._results import BacktestResult

    if not paths:
        raise ValueError("At least one path is required.")

    results = [BacktestResult.load(p) for p in paths]
    if labels is None:
        labels = [Path(p).name for p in paths]

    show(*results, labels=labels, title=title, open_browser=open_browser)
