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
import threading
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
    def set_stats(self, *, pnl: float, ret: float = 0.0,
                  win_rate: float | None = None) -> None: ...
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
    def set_stats(self, *, pnl: float, ret: float = 0.0,
                  win_rate: float | None = None) -> None: pass
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

    def __init__(self, *, n_markets: int, label: str | None = None) -> None:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            ProgressColumn,
            SpinnerColumn,
            TextColumn,
            TimeRemainingColumn,
        )
        from rich.table import Column
        from rich.text import Text

        class _PnLColumn(ProgressColumn):
            """Live stats on the Backtesting task (blank on other tasks).

            Fixed-width fields so the line never shrinks — VS Code's terminal
            renderer leaves stale trailing chars when a line gets shorter.
            """

            def render(self, task):
                pnl = task.fields.get("pnl")
                if pnl is None:
                    return Text("")
                ret = task.fields.get("ret") or 0.0
                win = task.fields.get("win")
                win_s = f"{win:>4.0%}" if win is not None else "  - "
                # Compact, fixed-width — keeps the line short so VS Code's
                # terminal doesn't wrap (which forces a new line per update).
                # no_wrap so a tight line crops this column rather than spilling
                # it onto a second row.
                s = f"PnL {pnl:>+8,.0f}  {ret:>+6.1%}  win {win_s}"
                return Text(s, style="green" if pnl >= 0 else "red", no_wrap=True)

        self._n_markets = n_markets
        # Optional strategy label appended to the "Backtesting" bar so
        # multi-strategy runs are distinguishable (e.g. "Backtesting Aggressive").
        self._label = label
        # In notebooks, force rich's terminal renderer (ANSI in-place updates)
        # rather than its Jupyter display renderer, which VS Code duplicates into
        # two flickering bars. Outside notebooks, write to stderr as usual.
        in_notebook = "ipykernel" in sys.modules or "IPython" in sys.modules
        self._notebook = in_notebook
        # In notebooks, notes are buffered and flushed after the bar finishes:
        # any print interleaved with the live bar leaves a ghost frame in VS Code.
        self._status_buffer: list[str] = []
        if in_notebook:
            # rich detects the cell width and crops to it, but a line that is
            # exactly the width hits the terminal's auto-wrap margin and spills
            # onto a second row — which VS Code can't redraw in place, so every
            # tick ghosts a new line. Reserve one column so the (possibly
            # labelled) bar always stays strictly under the edge; rich shrinks
            # the bar / ellipsizes the label to fit a single line.
            probe = Console(force_terminal=True, force_jupyter=False)
            console = Console(
                force_terminal=True, force_jupyter=False,
                width=max(20, probe.width - 1),
            )
        else:
            console = Console(stderr=True)
        self._progress = Progress(
            SpinnerColumn(),
            # Ellipsize a long label instead of wrapping it to a second row.
            TextColumn(
                "[bold]{task.description}",
                table_column=Column(no_wrap=True, overflow="ellipsis"),
            ),
            BarColumn(bar_width=16),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            _PnLColumn(table_column=Column(no_wrap=True, overflow="crop")),
            console=console,
            # Notebooks: remove the bar on completion. VS Code doesn't fully
            # clear rich's persisted final frame, leaving stale trailing chars.
            transient=in_notebook,
        )
        self._started = False
        self._mode = "stream"
        # Streams are prewarmed concurrently (see _prewarm_streams), so the
        # first ``market_started`` can fire from several threads at once.
        # Guard lazy task creation so we don't add duplicate bars.
        self._task_lock = threading.Lock()
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
        # Flush buffered notes after the live bar is gone (notebook path).
        if self._notebook and self._status_buffer:
            try:
                for msg in self._status_buffer:
                    print(f"· {msg}", file=sys.stderr, flush=True)
            except Exception:
                pass
            self._status_buffer.clear()

    def _start(self) -> None:
        if not self._started:
            self._progress.__enter__()
            self._started = True

    def set_mode(self, mode: str) -> None:
        self._mode = mode

    def set_stats(self, *, pnl: float, ret: float = 0.0,
                  win_rate: float | None = None) -> None:
        """Update the running stats shown on the Backtesting bar."""
        if self._consume_task is not None:
            self._progress.update(
                self._consume_task, pnl=pnl, ret=ret, win=win_rate,
            )

    def _ensure_event_tasks(self) -> None:
        if self._consume_task is not None:
            return
        with self._task_lock:
            # Double-checked: another prewarm thread may have created the
            # tasks between the unlocked check above and acquiring the lock.
            if self._consume_task is not None:
                return
            self._start()
            # A second concurrent bar means a multi-line live render, which VS
            # Code can't redraw in place (it re-emits a new line per tick). In
            # notebooks keep a single bar — the Backtesting bar sits at 0/N while
            # data streams in, then advances. Terminals handle multi-line fine.
            if self._mode != "replay" and not self._notebook:
                self._fetch_task = self._progress.add_task(
                    "Downloading", total=self._n_markets,
                )
            self._consume_task = self._progress.add_task(
                f"Backtesting {self._label}" if self._label else "Backtesting",
                total=self._n_markets,
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
        # Prefetch ``on_done`` callbacks fire from multiple prefetch threads,
        # so guard the counter to keep the "Downloading" bar from undercounting.
        with self._task_lock:
            self._fetched_markets += 1
            completed = self._fetched_markets
        if self._fetch_task is not None:
            self._progress.update(self._fetch_task, completed=completed)

    def market_finished(self, market_id: str) -> None:
        self._ensure_event_tasks()
        with self._task_lock:
            self._consumed_markets += 1
            completed = self._consumed_markets
        self._progress.update(self._consume_task, completed=completed)

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
        """Emit a status line.

        In notebooks the live bar renders on stdout, so notes go to stderr (a
        separate output block) — printing through the live console mid-render
        leaves a ghost bar in VS Code. In a real terminal the bar owns stderr,
        so once started we route through the rich console (which redraws around
        the print); otherwise plain stderr.
        """
        try:
            if self._notebook:
                # Buffer; flushed in __exit__ so nothing interleaves with the bar.
                self._status_buffer.append(message)
            elif self._started:
                self._progress.console.print(f"[dim]· {message}[/]")
            else:
                print(f"· {message}", file=sys.stderr)
        except Exception:
            pass


def make_reporter(
    *, enabled: bool = True, n_markets: int = 1, label: str | None = None,
) -> _ProgressReporter:
    """Build a reporter or a no-op.

    Falls back to no-op if ``rich`` is missing, if ``enabled`` is False,
    if ``MARKETLENS_PROGRESS`` is falsy, or if stderr isn't a TTY and we're
    not in Jupyter.

    ``label`` is appended to the "Backtesting" bar to distinguish strategies
    in a multi-strategy run.
    """
    if not enabled or _env_disabled() or not _can_render():
        return _NullReporter()
    try:
        import rich  # noqa: F401
    except ImportError:
        return _NullReporter()
    return _RichReporter(n_markets=n_markets, label=label)
