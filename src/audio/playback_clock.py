"""Shared, thread-safe playback clock fed by mpv.

mpv's reported ``time-pos`` is the only source of truth. Between two mpv updates the clock
extrapolates (bounded) from the last report so that video can pick frames at display rate;
every new report re-anchors it, so errors cannot accumulate.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class PlaybackClock:
    """Authoritative timeline for video and lyrics.

    Args:
        time_source: Monotonic time function (injectable for tests).
        max_extrapolation: Longest span (seconds) to extrapolate past the last mpv report.
    """

    def __init__(self, time_source: Callable[[], float] = time.monotonic,
                 max_extrapolation: float = 0.25) -> None:
        self._now = time_source
        self._max_extrap = max_extrapolation
        self._lock = threading.Lock()
        self._position = 0.0
        self._anchor = 0.0
        self._paused = True
        self._duration: float | None = None
        self._speed = 1.0
        self._has_position = False
        self._ended = False
        self._error: str | None = None
        self._last_report: float | None = None
        self._max_gap = 0.0

    def update(self, position: float | None = None, paused: bool | None = None,
               duration: float | None = None, speed: float | None = None) -> None:
        """Apply values reported by mpv (``None`` leaves a field unchanged)."""
        with self._lock:
            now = self._now()
            if position is not None:
                if self._last_report is not None and not self._paused:
                    self._max_gap = max(self._max_gap, now - self._last_report)
                self._last_report = now
                self._position = max(0.0, float(position))
                self._anchor = now
                self._has_position = True
            if paused is not None and paused != self._paused:
                # Freeze extrapolation at the pause point / restart it on resume.
                if paused:
                    self._position = self._extrapolated(now)
                self._anchor = now
                self._paused = paused
            if duration is not None and duration > 0:
                self._duration = float(duration)
            if speed is not None and speed > 0:
                self._position = self._extrapolated(now)
                self._anchor = now
                self._speed = float(speed)

    def _extrapolated(self, now: float) -> float:
        if self._paused or self._ended or not self._has_position:
            return self._position
        elapsed = min(max(0.0, now - self._anchor), self._max_extrap)
        return self._position + elapsed * self._speed

    def position(self) -> float:
        """Current playback position in seconds."""
        with self._lock:
            pos = self._extrapolated(self._now())
            if self._duration is not None:
                pos = min(pos, self._duration)
            return pos

    @property
    def max_update_gap(self) -> float:
        """Longest time between two mpv position reports while playing (diagnostics)."""
        with self._lock:
            return self._max_gap

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def duration(self) -> float | None:
        with self._lock:
            return self._duration

    @property
    def started(self) -> bool:
        """Whether mpv has reported at least one position."""
        with self._lock:
            return self._has_position

    @property
    def ended(self) -> bool:
        with self._lock:
            return self._ended

    @property
    def error(self) -> str | None:
        """Playback error message reported by the audio engine, if any."""
        with self._lock:
            return self._error

    def mark_ended(self) -> None:
        """Called when the audio engine reports end of file."""
        with self._lock:
            self._position = self._extrapolated(self._now())
            self._ended = True

    def set_error(self, message: str) -> None:
        """Record an audio engine failure (also marks playback as ended)."""
        with self._lock:
            self._error = message
            self._ended = True
