"""Runtime step logging for long lab e2e runs (visible with pytest -s)."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes")


def _inplace_progress() -> bool:
    """Overwrite the wait line in-place unless E2E_PROGRESS_NEWLINES=1.

    pytest often reports stderr as non-TTY even with -s, so an isatty() gate
    would keep spamming one line per poll.
    """
    return not _truthy_env("E2E_PROGRESS_NEWLINES")


def _step_confirm_enabled() -> bool:
    """Pause after each step when E2E_DEMO_MODE=true (pytest -s, interactive TTY)."""
    return _truthy_env("E2E_DEMO_MODE")


@dataclass
class Steps:
    """Numbered, timestamped progress lines for a single test."""

    title: str
    _n: int = field(default=0, init=False)
    _t0: float = field(default_factory=time.monotonic, init=False)
    _progress_open: bool = field(default=False, init=False)
    _progress_width: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        # pytest -s prints the test path on stdout with no trailing newline;
        # a stderr-only \n does not move that cursor. End that line on stdout.
        print(file=sys.stdout, flush=True)
        self._emit(f"=== {self.title} ===")

    def _elapsed(self) -> str:
        return f"{time.monotonic() - self._t0:7.1f}s"

    def _close_progress(self) -> None:
        if not self._progress_open:
            return
        print(file=sys.stderr, flush=True)
        self._progress_open = False
        self._progress_width = 0

    def _emit(self, msg: str, *, progress: bool = False) -> None:
        line = f"[{self._elapsed()}] {msg}"
        if progress and _inplace_progress():
            pad = max(self._progress_width - len(line), 0)
            print(f"\r{line}{' ' * pad}", end="", flush=True, file=sys.stderr)
            self._progress_open = True
            self._progress_width = max(self._progress_width, len(line))
            return

        self._close_progress()
        print(line, flush=True, file=sys.stderr)

    def _confirm_continue(self) -> None:
        """Block until the operator types y/Y (only when E2E_DEMO_MODE is set)."""
        if not _step_confirm_enabled():
            return
        self._close_progress()
        if not sys.stdin.isatty():
            self._emit(
                "       (E2E_DEMO_MODE set but stdin is not a TTY — continuing)"
            )
            return
        while True:
            # Prompt on stderr next to other step lines; read from stdin.
            print("       Continue? [y/Y]: ", end="", flush=True, file=sys.stderr)
            try:
                answer = sys.stdin.readline()
            except EOFError as exc:
                raise RuntimeError(
                    "E2E_DEMO_MODE: EOF while waiting for y/Y"
                ) from exc
            if answer == "":
                raise RuntimeError(
                    "E2E_DEMO_MODE: EOF while waiting for y/Y"
                )
            if answer.strip() in ("y", "Y"):
                return
            print(
                "       type y or Y to continue",
                flush=True,
                file=sys.stderr,
            )

    def step(self, msg: str) -> None:
        self._n += 1
        self._emit(f"STEP {self._n}: {msg}")
        self._confirm_continue()

    def info(self, msg: str) -> None:
        self._emit(f"       {msg}")

    def progress(self, msg: str) -> None:
        """Overwrite the same wait line; set E2E_PROGRESS_NEWLINES=1 for one line per tick."""
        self._emit(f"       {msg}", progress=True)

    def ok(self, msg: str = "done") -> None:
        self._emit(f"       ✓ {msg}")

    def done(self, msg: str = "finished") -> None:
        self._emit(f"=== {self.title}: {msg} ({self._n} steps) ===")
