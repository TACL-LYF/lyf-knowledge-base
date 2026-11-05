"""Stage-by-stage progress output for long-running commands.

    [3/8] Reading Discord…
          #camp-feedback: 4 new channel messages
          threads: 2/2 with new messages read
          ✓ 7 new messages in 3 items (2.4s)

Each stage prints a numbered header when it starts, indented detail lines while it
runs, and a result line (✓ with its summary, or ✗ with the error) when it ends.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

Log = Callable[[str], None]
INDENT = "      "


class Stage:
    """Handle for the running stage: set `result` to the summary shown when it ends."""

    def __init__(self, stages: Stages) -> None:
        self._stages = stages
        self.result = "done"

    def detail(self, msg: str) -> None:
        self._stages.detail(msg)


class Stages:
    def __init__(self, log: Log, total: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.log = log
        self.total = total
        self.n = 0
        self._clock = clock
        self._started = clock()

    def detail(self, msg: str) -> None:
        for line in msg.splitlines() or [""]:
            self.log(INDENT + line)

    @contextmanager
    def stage(self, title: str) -> Iterator[Stage]:
        self.n += 1
        self.log(f"[{self.n}/{self.total}] {title}…")
        handle = Stage(self)
        t0 = self._clock()
        try:
            yield handle
        except BaseException as e:
            msg = str(e) or type(e).__name__
            self.log(f"{INDENT}✗ failed after {self._clock() - t0:.1f}s: {msg}")
            raise
        self.log(f"{INDENT}✓ {handle.result} ({self._clock() - t0:.1f}s)")

    def skip(self, title: str, reason: str) -> None:
        self.n += 1
        self.log(f"[{self.n}/{self.total}] {title}: skipped ({reason})")

    def elapsed(self) -> float:
        return self._clock() - self._started
