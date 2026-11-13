"""Clock-driven frame selection: show the frame that belongs to the audio position."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import numpy as np

from src.video.video_loader import VideoLoader

#: Gaps larger than this many frames are crossed with a seek instead of decode-skipping.
DEFAULT_SEEK_THRESHOLD = 24
#: The clock may report a slightly earlier time than an extrapolated one it showed before;
#: backward moves up to this many frames are ignored (a real seek-back is seconds long).
BACKWARD_TOLERANCE = 15


def target_frame_index(position: float, fps: float, frame_count: int = 0) -> int:
    """Index of the frame that should be on screen at *position* seconds."""
    if fps <= 0 or not math.isfinite(position):
        return 0
    index = max(0, int(position * fps + 1e-9))
    if frame_count > 0:
        index = min(index, frame_count - 1)
    return index


class SyncAction(Enum):
    """What must be done to reach the target frame."""

    HOLD = "hold"  # target is the frame already shown (or past the end)
    READ = "read"  # next frame is the target
    SKIP = "skip"  # decode-skip a few frames, then read
    SEEK = "seek"  # jump (backward move, or a large forward gap)


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """Decision for one clock reading. ``skip`` is the number of frames to discard first."""

    action: SyncAction
    target: int
    skip: int = 0


def plan_sync(target: int, next_index: int, seek_threshold: int = DEFAULT_SEEK_THRESHOLD,
              backward_tolerance: int = BACKWARD_TOLERANCE) -> SyncPlan:
    """Decide how to reach frame *target* when the loader will next read *next_index*.

    Pure function, so the catch-up policy is unit-testable without any video.
    """
    shown = next_index - 1
    if target == shown:
        return SyncPlan(SyncAction.HOLD, target)
    if target < shown:
        if shown - target <= backward_tolerance:
            return SyncPlan(SyncAction.HOLD, target)
        return SyncPlan(SyncAction.SEEK, target)
    gap = target - next_index
    if gap == 0:
        return SyncPlan(SyncAction.READ, target)
    if gap > seek_threshold:
        return SyncPlan(SyncAction.SEEK, target)
    return SyncPlan(SyncAction.SKIP, target, skip=gap)


class SyncedVideoSource:
    """Wraps an open :class:`VideoLoader` and serves frames chosen by the shared clock.

    Frames are never "played" at their own pace: each call looks at the clock position and
    returns the frame for that instant, dropping everything in between. A slow renderer
    therefore skips frames instead of accumulating delay.

    A failed read in the middle of the video (a dropped network connection, a damaged
    packet) is *not* treated as the end: the source reopens the stream at the target frame,
    retrying with a growing back-off, and only gives up at the real end of the video.
    """

    RETRY_DELAYS = (0.0, 0.5, 1.0, 2.0, 4.0)  # seconds between recovery attempts (last repeats)

    def __init__(self, loader: VideoLoader, seek_threshold: int = DEFAULT_SEEK_THRESHOLD,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._loader = loader
        self._seek_threshold = seek_threshold
        self._now = clock
        self._next_index = 0  # the loader holds a pending frame 0 after open()
        self._failures = 0
        self._retry_at = 0.0
        self._reopened = False  # reconnected; counted as a recovery once frames flow again
        self.exhausted = False  # real end of the video reached
        self.recovering = False  # reads are failing and we are retrying
        self.frames_decoded = 0
        self.frames_skipped = 0
        self.seeks = 0
        self.recoveries = 0

    @property
    def fps(self) -> float:
        return self._loader.info.fps

    @property
    def shown_index(self) -> int:
        """Index of the most recently returned frame (``-1`` if none)."""
        return self._next_index - 1

    def _at_real_end(self, target: int) -> bool:
        count = self._loader.info.frame_count
        return count > 0 and max(target, self._next_index) >= count - 1

    def _recover(self, target: int) -> bool:
        """Reopen the stream positioned at *target*. Rate-limited; returns success."""
        now = self._now()
        if now < self._retry_at:
            return False
        delay = self.RETRY_DELAYS[min(self._failures, len(self.RETRY_DELAYS) - 1)]
        self._retry_at = now + delay
        self._failures += 1
        if self._loader.reopen(target):
            self._next_index = target
            self._reopened = True
            return True
        self.recovering = True
        return False

    def frame_at(self, position: float) -> tuple[int, np.ndarray] | None:
        """Return ``(index, frame)`` if a *new* frame is due at *position*, else ``None``."""
        info = self._loader.info
        target = target_frame_index(position, info.fps, info.frame_count)
        plan = plan_sync(target, self._next_index, self._seek_threshold)
        if self.exhausted and plan.action is not SyncAction.SEEK:
            return None
        if self.recovering:
            # Streams are failing: skip straight to a reopen at the current target.
            if not self._recover(target):
                return None
        elif plan.action is SyncAction.HOLD:
            return None
        elif plan.action is SyncAction.SEEK:
            if not self._loader.seek(plan.target) and not self._recover(plan.target):
                return None
            self.seeks += 1
            self._next_index = plan.target
            self.exhausted = False
        elif plan.action is SyncAction.SKIP:
            skipped = self._loader.skip(plan.skip)
            self._next_index += skipped
            self.frames_skipped += skipped
            if skipped < plan.skip:
                self._read_failed(target)
                return None
        ok, frame = self._loader.read()
        if not ok or frame is None:
            self._read_failed(target)
            return None
        self._failures = 0
        self.recovering = False
        if self._reopened:
            self._reopened = False
            self.recoveries += 1
        index = self._next_index
        self._next_index += 1
        self.frames_decoded += 1
        return index, frame

    def _read_failed(self, target: int) -> None:
        """A read failed: real end of video, or a broken stream to recover."""
        if self._at_real_end(target):
            self.exhausted = True
        else:
            self.recovering = True
            self._retry_at = min(self._retry_at, self._now())  # first retry happens immediately


class BackgroundVideoSource:
    """Runs a :class:`SyncedVideoSource` on its own thread.

    Network reads can block for seconds; doing them on the UI thread freezes the whole
    screen. The worker keeps asking the shared clock which frame is due and publishes the
    newest one; :meth:`take` hands it to the UI thread without ever blocking.

    Args:
        source: The synchronous source to drive.
        position: Callable returning the shared clock position in seconds.
        pause: Callable; while it returns ``True`` the worker idles (e.g. video hidden).
    """

    def __init__(self, source: SyncedVideoSource, position: Callable[[], float],
                 pause: Callable[[], bool] = lambda: False) -> None:
        self.source = source
        self._position = position
        self._pause = pause
        self._lock = threading.Lock()
        self._ready: tuple[int, np.ndarray] | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="video-decode", daemon=True)

    def start(self) -> None:
        """Start decoding."""
        self._thread.start()

    def stop(self) -> None:
        """Stop the worker and wait for it (so the caller can safely release the loader)."""
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=10.0)

    def take(self) -> tuple[int, np.ndarray] | None:
        """Return the newest frame not yet taken, or ``None``. Never blocks on I/O."""
        with self._lock:
            ready, self._ready = self._ready, None
        return ready

    def _run(self) -> None:
        interval = 0.004
        while not self._stop.is_set():
            if self._pause():
                self._stop.wait(0.05)
                continue
            try:
                got = self.source.frame_at(self._position())
            except Exception:  # never let a decoder error kill the thread silently
                got = None
                self._stop.wait(0.25)
            if got is not None:
                with self._lock:
                    self._ready = got
            else:
                self._stop.wait(interval)
