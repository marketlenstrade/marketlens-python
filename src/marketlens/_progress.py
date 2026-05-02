"""Optional rich-based progress reporting for backtests and downloads.

The reporter is a no-op when:
  - rich isn't installed
  - the user passed enabled=False
  - MARKETLENS_PROGRESS env var is set to a falsy value ("0", "false", "no")
  - stderr isn't a TTY and we're not in Jupyter
"""
from __future__ import annotations

import os
import sys
from typing import Protocol


def _env_disabled() -> bool:
    val = os.environ.get("MARKETLENS_PROGRESS")
    return val is not None and val.strip().lower() in {"0", "false", "no", "off"}


def _can_render() -> bool:
    if "ipykernel" in sys.modules or "IPython" in sys.modules:
        return True
    try:
        return sys.stderr.isatty()
    except Exception:
        return False


class _ProgressReporter(Protocol):
    def __enter__(self) -> "_ProgressReporter": ...
    def __exit__(self, *args: object) -> None: ...
    def fetched(self, market_id: str, n: int) -> None: ...
    def consumed(self, market_id: str, n: int) -> None: ...
    def market_started(self, market_id: str, label: str) -> None: ...
    def market_fetch_done(self, market_id: str) -> None: ...
    def market_finished(self, market_id: str) -> None: ...
    def download_started(self, label: str, total_bytes: int | None) -> None: ...
    def download_progress(self, n_bytes: int) -> None: ...
    def download_finished(self) -> None: ...
    def status(self, message: str) -> None: ...


class _NullReporter:
    """No-op reporter. All methods are cheap stubs."""

    def __enter__(self) -> "_NullReporter":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def fetched(self, market_id: str, n: int) -> None: pass
    def consumed(self, market_id: str, n: int) -> None: pass
    def market_started(self, market_id: str, label: str) -> None: pass
    def market_fetch_done(self, market_id: str) -> None: pass
    def market_finished(self, market_id: str) -> None: pass
    def download_started(self, label: str, total_bytes: int | None) -> None: pass
    def download_progress(self, n_bytes: int) -> None: pass
    def download_finished(self) -> None: pass
    def status(self, message: str) -> None: pass


class _RichReporter:
    """Multi-bar reporter backed by rich.progress.Progress.

    Bars are denominated in markets:
      - "Fetching"     — markets whose data has been fully fetched / N
      - "Backtesting"  — markets whose events have been fully consumed / N
      - "Downloading {label}" — one-off byte/event download bars.

    Markets is the unit because the count is known instantly after
    resolution (no per-market HTTP) and is consistent across every
    backtest shape. Single-market runs go 0/1 → 1/1; multi-market
    runs are granular by market.
    """

    def __init__(self, *, n_markets: int) -> None:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TimeRemainingColumn,
        )

        self._n_markets = n_markets
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=Console(stderr=True),
            transient=False,
        )
        self._fetched_markets = 0
        self._consumed_markets = 0
        self._fetched_events = 0
        self._consumed_events = 0
        self._fetch_task: int | None = None
        self._consume_task: int | None = None
        self._download_task: int | None = None

    def __enter__(self) -> "_RichReporter":
        self._progress.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        self._progress.__exit__(*args)

    def _ensure_event_tasks(self) -> None:
        if self._fetch_task is None:
            self._fetch_task = self._progress.add_task(
                "Fetching", total=self._n_markets,
            )
            self._consume_task = self._progress.add_task(
                "Backtesting", total=self._n_markets,
            )

    def fetched(self, market_id: str, n: int) -> None:
        # Tally only — bars advance on ``market_fetch_done`` / ``market_finished``.
        self._fetched_events += n

    def consumed(self, market_id: str, n: int) -> None:
        self._consumed_events += n

    def market_started(self, market_id: str, label: str) -> None:
        self._ensure_event_tasks()

    def market_fetch_done(self, market_id: str) -> None:
        self._ensure_event_tasks()
        self._fetched_markets += 1
        self._progress.update(
            self._fetch_task, completed=self._fetched_markets,
        )

    def market_finished(self, market_id: str) -> None:
        self._ensure_event_tasks()
        self._consumed_markets += 1
        self._progress.update(
            self._consume_task, completed=self._consumed_markets,
        )

    def download_started(self, label: str, total_bytes: int | None) -> None:
        if self._download_task is not None:
            self._progress.remove_task(self._download_task)
        self._download_task = self._progress.add_task(
            f"Downloading {label}", total=total_bytes,
        )

    def download_progress(self, n_bytes: int) -> None:
        if self._download_task is not None:
            self._progress.update(self._download_task, completed=n_bytes)

    def download_finished(self) -> None:
        # Final byte count update is the caller's responsibility; rich will
        # mark the bar complete when completed >= total.
        pass

    def status(self, message: str) -> None:
        """Print a status line above the bars during prep phases."""
        try:
            self._progress.console.print(f"[dim]· {message}[/]")
        except Exception:
            pass


def make_reporter(*, enabled: bool = True, n_markets: int = 1) -> _ProgressReporter:
    """Build a reporter or a no-op.

    Falls back to no-op if ``rich`` is missing, if ``enabled`` is False,
    if ``MARKETLENS_PROGRESS`` is falsy, or if stderr isn't a TTY and we're
    not in Jupyter.
    """
    if not enabled or _env_disabled() or not _can_render():
        return _NullReporter()
    try:
        import rich  # noqa: F401
    except ImportError:
        return _NullReporter()
    return _RichReporter(n_markets=n_markets)
