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
    def set_mode(self, mode: str) -> None: ...
    def batch_download_started(self, label: str, total: int) -> None: ...
    def batch_download_advance(self) -> None: ...


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
    def set_mode(self, mode: str) -> None: pass
    def batch_download_started(self, label: str, total: int) -> None: pass
    def batch_download_advance(self) -> None: pass


class _RichReporter:
    """Multi-bar reporter backed by rich.progress.Progress.

    Bars are denominated in markets:
      - "Downloading"  — markets whose data has been pulled / N
                         (skipped in ``replay`` mode where data is on disk;
                         also drives the bulk-export aggregate bar)
      - "Backtesting"  — markets whose events have been fully consumed / N
      - per-file byte bar — single-file download when no aggregate is set.

    The underlying ``Progress`` container is entered lazily on first
    task creation so the user doesn't see an empty bar during the
    HTTP round-trip that precedes the first byte.
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
        self._started = False
        self._mode = "stream"
        self._fetched_markets = 0
        self._consumed_markets = 0
        self._fetched_events = 0
        self._consumed_events = 0
        self._fetch_task: int | None = None
        self._consume_task: int | None = None
        self._download_task: int | None = None
        self._batch_task: int | None = None

    def __enter__(self) -> "_RichReporter":
        return self

    def __exit__(self, *args: object) -> None:
        if self._started:
            self._progress.__exit__(*args)

    def _start(self) -> None:
        if not self._started:
            self._progress.__enter__()
            self._started = True

    def set_mode(self, mode: str) -> None:
        self._mode = mode

    def _ensure_event_tasks(self) -> None:
        if self._consume_task is not None:
            return
        self._start()
        if self._mode != "replay":
            self._fetch_task = self._progress.add_task(
                "Downloading", total=self._n_markets,
            )
        self._consume_task = self._progress.add_task(
            "Backtesting", total=self._n_markets,
        )

    def fetched(self, market_id: str, n: int) -> None:
        # Tally only; bars advance on ``market_fetch_done`` / ``market_finished``.
        self._fetched_events += n

    def consumed(self, market_id: str, n: int) -> None:
        self._consumed_events += n

    def market_started(self, market_id: str, label: str) -> None:
        self._ensure_event_tasks()

    def market_fetch_done(self, market_id: str) -> None:
        self._ensure_event_tasks()
        self._fetched_markets += 1
        if self._fetch_task is not None:
            self._progress.update(
                self._fetch_task, completed=self._fetched_markets,
            )

    def market_finished(self, market_id: str) -> None:
        self._ensure_event_tasks()
        self._consumed_markets += 1
        self._progress.update(
            self._consume_task, completed=self._consumed_markets,
        )

    def batch_download_started(self, label: str, total: int) -> None:
        if total <= 0:
            return
        self._start()
        self._batch_task = self._progress.add_task(label, total=total)

    def batch_download_advance(self) -> None:
        if self._batch_task is not None:
            self._progress.advance(self._batch_task, 1)

    def download_started(self, label: str, total_bytes: int | None) -> None:
        # When an aggregate batch bar is active, per-file byte bars would
        # just churn underneath it; suppress them.
        if self._batch_task is not None:
            return
        self._start()
        if self._download_task is not None:
            self._progress.remove_task(self._download_task)
        self._download_task = self._progress.add_task(
            f"Downloading {label}", total=total_bytes,
        )

    def download_progress(self, n_bytes: int) -> None:
        if self._batch_task is not None or self._download_task is None:
            return
        self._progress.update(self._download_task, completed=n_bytes)

    def download_finished(self) -> None:
        # Final byte count update is the caller's responsibility; rich will
        # mark the bar complete when completed >= total.
        pass

    def status(self, message: str) -> None:
        """Print a status line above the bars during prep phases."""
        try:
            if self._started:
                self._progress.console.print(f"[dim]· {message}[/]")
            else:
                print(f"· {message}", file=sys.stderr)
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
