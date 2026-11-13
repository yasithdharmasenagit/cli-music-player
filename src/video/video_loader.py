"""Open local video files and read frames via OpenCV."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import cv2
import numpy as np

from src.core.exceptions import VideoLoadError
from src.utils.logger import get_logger

_log = get_logger("video_loader")
DEFAULT_FPS = 30.0


@dataclass(frozen=True, slots=True)
class VideoInfo:
    """Basic metadata of an opened video."""

    path: Path | str
    fps: float
    width: int
    height: int
    frame_count: int  # 0 when the container does not report it

    @property
    def duration(self) -> float:
        """Duration in seconds (0.0 if unknown)."""
        return self.frame_count / self.fps if self.frame_count > 0 else 0.0


class _DeadCapture:
    """Stand-in used after a failed reopen: every read fails, so the caller keeps retrying."""

    def isOpened(self) -> bool:
        return False

    def get(self, prop: int) -> float:
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        return False

    def read(self) -> tuple[bool, None]:
        return False, None

    def grab(self) -> bool:
        return False

    def release(self) -> None:
        return None


class VideoLoader:
    """Reads frames from a local video file. Usable as a context manager.

    Args:
        path: Video file path.
        capture_factory: Callable creating a ``cv2.VideoCapture``-like object from a string
            path (injectable for tests).
    """

    def __init__(self, path: str | Path,
                 capture_factory: Callable[[str], Any] = cv2.VideoCapture) -> None:
        text = str(path)
        #: Network streams (http/https) are opened directly; everything else is a file.
        self.is_stream = text.lower().startswith(("http://", "https://"))
        self.path: Path | str = text if self.is_stream else Path(path)
        self._factory = capture_factory
        self._cap: Any | None = None
        self._pending: np.ndarray | None = None
        self._info: VideoInfo | None = None

    @property
    def is_open(self) -> bool:
        """Whether a capture is currently held."""
        return self._cap is not None

    @property
    def info(self) -> VideoInfo:
        """Metadata; only valid after :meth:`open`."""
        if self._info is None:
            raise VideoLoadError("Video is not open.")
        return self._info

    def open(self) -> VideoInfo:
        """Open and validate the file.

        Raises:
            VideoLoadError: if the file is missing, empty, not a decodable video, or has
                no readable frames.
        """
        if self._cap is not None:
            return self.info
        if not isinstance(self.path, str):
            if not self.path.is_file():
                raise VideoLoadError(f"Video file not found: {self.path}")
            if self.path.stat().st_size == 0:
                raise VideoLoadError(f"Video file is empty: {self.path}")
        try:
            cap = self._factory(str(self.path))
        except Exception as exc:  # OpenCV can raise cv2.error
            raise VideoLoadError(f"Could not open video: {exc}") from exc
        try:
            if not cap.isOpened():
                raise VideoLoadError(f"Not a valid or supported video file: {self._label}")
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(cv2.CAP_PROP_FPS))
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            ok, first = cap.read()
            if not ok or first is None or first.size == 0:
                raise VideoLoadError(f"Video contains no readable frames: {self._label}")
            if width <= 0 or height <= 0:
                height, width = first.shape[:2]
            if not math.isfinite(fps) or fps <= 0:
                fps = DEFAULT_FPS
            self._info = VideoInfo(self.path, fps, width, height, max(count, 0))
        except BaseException:
            cap.release()
            raise
        self._cap = cap
        self._pending = first
        _log.info("Opened %s", self._label)
        return self._info

    @property
    def _label(self) -> str:
        """Safe display name: stream URLs carry access tokens and are never shown."""
        return "network stream" if isinstance(self.path, str) else self.path.name

    def seek(self, frame_index: int) -> bool:
        """Jump so the next :meth:`read` returns frame *frame_index*. Returns success."""
        cap = self._require_open()
        self._pending = None
        return bool(cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_index)))

    def reopen(self, frame_index: int = 0) -> bool:
        """Drop the current capture, open the source again and position at *frame_index*.

        Used to recover from a broken network connection. Returns ``True`` if the new capture
        is open and positioned; on failure the loader stays "open" so a later call can retry.
        """
        self._require_open()
        self._pending = None
        old, self._cap = self._cap, None
        if old is not None:
            try:
                old.release()
            except Exception:  # a dead capture may complain; we are replacing it anyway
                pass
        try:
            cap = self._factory(str(self.path))
        except Exception:
            self._cap = _DeadCapture()
            return False
        self._cap = cap
        try:
            if not cap.isOpened():
                return False
            return frame_index <= 0 or bool(cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index))
        except Exception:
            return False

    def read(self) -> tuple[bool, np.ndarray | None]:
        """Read the next frame. Returns ``(False, None)`` at end of file."""
        cap = self._require_open()
        if self._pending is not None:
            frame, self._pending = self._pending, None
            return True, frame
        ok, frame = cap.read()
        if not ok or frame is None:
            return False, None
        return True, frame

    def skip(self, count: int = 1) -> int:
        """Advance *count* frames without fully decoding them. Returns frames skipped."""
        cap = self._require_open()
        skipped = 0
        while skipped < count:
            if self._pending is not None:
                self._pending = None
            elif not cap.grab():
                break
            skipped += 1
        return skipped

    def close(self) -> None:
        """Release the underlying capture. Safe to call repeatedly."""
        cap, self._cap = self._cap, None
        self._pending = None
        if cap is not None:
            cap.release()

    def _require_open(self) -> Any:
        if self._cap is None:
            raise VideoLoadError("Video is not open.")
        return self._cap

    def __enter__(self) -> VideoLoader:
        self.open()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    def __del__(self) -> None:  # last-resort safety net
        try:
            self.close()
        except Exception:
            pass
