"""Estimate download speed and remaining time from polled byte counts.

Component downloads only expose "bytes on disk so far".  Each status read
records one sample; the speed is the byte growth over the recent window, so a
stall or a slow mirror shows up within seconds instead of being averaged away
by the whole download.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Tuple

# Too short a span turns one burst into a wild estimate.
MIN_SPAN_SECONDS = 2.0


class TransferRate:
    def __init__(self, window_seconds: float = 20.0) -> None:
        self._window = float(window_seconds)
        self._samples: Deque[Tuple[float, int]] = deque()

    def reset(self) -> None:
        self._samples.clear()

    def observe(self, now: float, transferred: int) -> None:
        """Record ``transferred`` bytes at monotonic time ``now``."""

        transferred = max(0, int(transferred))
        if self._samples and transferred < self._samples[-1][1]:
            # A retry or another mirror restarted the file.
            self._samples.clear()
        self._samples.append((float(now), transferred))
        while len(self._samples) > 2 and now - self._samples[0][0] > self._window:
            self._samples.popleft()

    def bytes_per_second(self) -> Optional[float]:
        if len(self._samples) < 2:
            return None
        (start, first), (end, last) = self._samples[0], self._samples[-1]
        span = end - start
        if span < MIN_SPAN_SECONDS:
            return None
        return max(0.0, (last - first) / span)

    def eta_seconds(self, total: int) -> Optional[int]:
        """Seconds until ``total`` bytes at the recent speed; ``None`` if unknown."""

        speed = self.bytes_per_second()
        if not speed or not self._samples or total <= 0:
            return None
        remaining = max(0, int(total) - self._samples[-1][1])
        return int(round(remaining / speed))
