"""Clock-driven frame selection: show the frame that belongs to the audio position."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
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
    #: Local files are not flaky: after this many failed reopens the video simply ended early.
    LOCAL_MAX_ATTEMPTS = 3

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
        if not getattr(self._loader, "is_stream", True) and self._failures >= self.LOCAL_MAX_ATTEMPTS:
            self.exhausted = True  # damaged/short local file: stop retrying, hold the last frame
            self.recovering = False
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

    @property
    def next_index(self) -> int:
        """Index of the frame the next sequential read will return."""
        return self._next_index

    @property
    def frame_count(self) -> int:
        """Total frames reported by the container (0 if unknown)."""
        return self._loader.info.frame_count

    @property
    def frame_bytes(self) -> int:
        """Size of one decoded BGR frame (for sizing the decode-ahead buffer)."""
        info = self._loader.info
        return max(1, info.width * info.height * 3)

    def seconds_until_next_frame(self, position: float) -> float:
        """Time until the frame after the one shown becomes due (0 if it already is)."""
        fps = self._loader.info.fps
        return max(0.0, (self._next_index) / fps - position) if fps > 0 else 0.0

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
    """Runs a :class:`SyncedVideoSource` on its own thread, decoding a little ahead.

    Network reads can block for seconds, and decoding on the UI thread freezes the screen. A
    worker thread therefore decodes frames into a small bounded buffer; the UI thread picks
    the frame that is due *by the shared clock* with :meth:`take`, which never blocks on I/O.
    Because delivery happens on the UI thread, a decode burst can never delay a frame.

    Decoding a few frames back-to-back and then idling uses measurably less CPU than decoding
    one frame per frame period (the core stays awake and warm). The buffer is bounded by
    ``buffer_frames`` and ``max_buffer_bytes`` so memory use stays flat, and look-ahead never
    affects sync because frames are released only when the clock says they are due.

    Args:
        source: The synchronous source to drive.
        position: Callable returning the shared clock position in seconds.
        pause: Callable; while it returns ``True`` the worker idles (e.g. video hidden).
        buffer_frames: Decode-ahead capacity in frames (1 disables look-ahead).
        low_watermark: A new burst starts when at most this many frames are buffered.
        max_buffer_bytes: Upper bound for the look-ahead buffer (frames can be large).
    """

    def __init__(self, source: SyncedVideoSource, position: Callable[[], float],
                 pause: Callable[[], bool] = lambda: False, buffer_frames: int = 8,
                 low_watermark: int = 3, max_buffer_bytes: int = 32 * 2**20) -> None:
        self.source = source
        self._position = position
        self._pause = pause
        self.capacity = max(1, min(buffer_frames, max_buffer_bytes // source.frame_bytes))
        self.low_watermark = max(0, min(low_watermark, self.capacity - 1))
        self._lock = threading.Lock()
        self._buffer: deque[tuple[int, np.ndarray]] = deque()  # decoded, not yet taken
        self._frame_ready = threading.Event()  # worker -> UI: the buffer gained frames
        self._wake_worker = threading.Event()  # UI -> worker: the buffer has room again
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="video-decode", daemon=True)
        self.delivered = 0  # frames handed to the UI (decoded-ahead frames are not counted)
        self.last_delivered = -1  # index of the frame most recently handed to the UI

    @property
    def buffered(self) -> int:
        """Frames currently decoded and waiting (never exceeds :attr:`capacity`)."""
        with self._lock:
            return len(self._buffer)

    def start(self) -> None:
        """Start decoding."""
        self._thread.start()

    def stop(self) -> None:
        """Stop the worker and wait for it (so the caller can safely release the loader)."""
        self._stop.set()
        self._wake_worker.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=10.0)
        with self._lock:
            self._buffer.clear()

    # -- UI side

    def take(self) -> tuple[int, np.ndarray] | None:
        """Return the newest frame that is due at the clock's position, or ``None``.

        Older due frames are dropped (the picture catches up). Never blocks on I/O.
        """
        src = self.source
        target = target_frame_index(self._position(), src.fps, src.frame_count)
        due: tuple[int, np.ndarray] | None = None
        with self._lock:
            while self._buffer and self._buffer[0][0] <= target:
                due = self._buffer.popleft()
        if due is not None:
            self.delivered += 1
            self.last_delivered = due[0]
            self._wake_worker.set()  # room for more look-ahead
        return due

    def seconds_until_next(self) -> float | None:
        """Time until the earliest buffered frame is due (``None`` if nothing is buffered)."""
        with self._lock:
            first = self._buffer[0][0] if self._buffer else None
        if first is None:
            return None
        return max(0.0, first / self.source.fps - self._position())

    def wait_for_frame(self, timeout: float) -> bool:
        """Sleep until a buffered frame is due, the worker adds frames, or *timeout* passes.

        Lets the UI loop idle instead of polling. Returns True if woken by the worker.
        """
        nxt = self.seconds_until_next()
        wait = timeout if nxt is None else min(timeout, max(0.001, nxt))
        woken = self._frame_ready.wait(wait)
        if woken:
            self._frame_ready.clear()
        return woken

    # -- worker side

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._pause():
                with self._lock:
                    self._buffer.clear()  # stale once the video is shown again
                self._stop.wait(0.05)
                continue
            try:
                wait = self._step()
            except Exception:  # never let a decoder error kill the thread silently
                with self._lock:
                    self._buffer.clear()
                wait = 0.25
            self._wake_worker.wait(wait)
            self._wake_worker.clear()

    def _jumped(self, target: int) -> bool:
        """True if the clock moved somewhere decoding cannot reach sequentially."""
        src = self.source
        shown = src.next_index - 1
        return target < shown - BACKWARD_TOLERANCE or target - src.next_index > DEFAULT_SEEK_THRESHOLD

    def _step(self) -> float:
        """One scheduling step. Returns how long to sleep before the next one."""
        src = self.source
        pos = self._position()
        target = target_frame_index(pos, src.fps, src.frame_count)
        with self._lock:
            if self._buffer and self._buffer[0][0] > target + self.capacity + BACKWARD_TOLERANCE:
                self._buffer.clear()  # clock jumped backwards past everything decoded ahead
            buffered = len(self._buffer)
        if self._jumped(target):
            with self._lock:
                self._buffer.clear()
            self._decode_into(pos, 1, exact=True)  # seek / catch up straight to the clock
        elif not src.exhausted and buffered <= self.low_watermark:
            self._decode_into(pos, self.capacity - buffered)
        if src.recovering or src.exhausted:
            return 0.1
        return 0.05  # woken early by take() as soon as the buffer drains

    def _decode_into(self, pos: float, count: int, exact: bool = False) -> None:
        """Decode up to *count* frames back-to-back, sequentially (or catching up first).

        With ``exact`` the first request is for the clock's own position, which seeks when the
        clock jumped (including backwards); otherwise reads continue sequentially.
        """
        src = self.source
        added = False
        for i in range(max(0, count)):
            if self._stop.is_set():
                break
            # Sequential read when ahead of the clock; a catch-up skip/seek when behind it.
            at = pos if exact and i == 0 else max(pos, (src.next_index + 0.5) / src.fps)
            got = src.frame_at(at)
            if got is None:
                break
            with self._lock:
                self._buffer.append(got)
            added = True
            self._frame_ready.set()  # let the UI see the first frames of a burst immediately
        if added:
            self._frame_ready.set()
