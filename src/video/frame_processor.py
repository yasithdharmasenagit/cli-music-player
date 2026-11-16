"""Frame validation, resizing and grayscale conversion."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from src.core.exceptions import InvalidFrameError

_INTERPOLATION = {"area": cv2.INTER_AREA, "linear": cv2.INTER_LINEAR}


@dataclass(slots=True)
class ProcessedFrame:
    """A frame reduced to terminal-grid resolution.

    Attributes:
        gray: ``(rows, cols)`` uint8 brightness grid.
        color: ``(rows, cols, 3)`` uint8 BGR grid, or ``None`` if colour was not requested.

    When the producing :class:`FrameProcessor` reuses buffers, the arrays are only valid
    until its next ``process`` call.
    """

    gray: np.ndarray
    color: np.ndarray | None = None

    @property
    def rows(self) -> int:
        return int(self.gray.shape[0])

    @property
    def cols(self) -> int:
        return int(self.gray.shape[1])


class FrameProcessor:
    """Resizes frames to a character grid and converts them to grayscale.

    Args:
        width: Output width in characters.
        char_aspect: Terminal cell width/height ratio (about 0.5: cells are twice as tall as wide).
        keep_color: Also return the resized BGR grid.
        max_rows: Optional cap on output rows; width shrinks to fit while keeping the aspect.
        interpolation: ``"area"`` (anti-aliased, best for large reductions) or ``"linear"``
            (about 100x cheaper, but aliases when shrinking a lot).
        reuse_buffers: Write results into preallocated arrays instead of allocating per frame.
            Results are then only valid until the next call (the converter uses them at once).
    """

    def __init__(self, width: int = 120, char_aspect: float = 0.5,
                 keep_color: bool = False, max_rows: int | None = None,
                 interpolation: str = "area", reuse_buffers: bool = False) -> None:
        if width < 1:
            raise ValueError("width must be >= 1")
        if char_aspect <= 0:
            raise ValueError("char_aspect must be positive")
        if interpolation not in _INTERPOLATION:
            raise ValueError(f"interpolation must be one of {sorted(_INTERPOLATION)}")
        self.width = width
        self.char_aspect = char_aspect
        self.keep_color = keep_color
        self.max_rows = max_rows
        self.interpolation = interpolation
        self.reuse_buffers = reuse_buffers
        self._small: np.ndarray | None = None
        self._gray: np.ndarray | None = None

    def target_size(self, src_width: int, src_height: int) -> tuple[int, int]:
        """Return ``(cols, rows)`` for a source of the given pixel size."""
        if src_width < 1 or src_height < 1:
            raise InvalidFrameError("Frame has zero width or height.")
        cols = self.width
        rows = max(1, round(src_height / src_width * cols * self.char_aspect))
        if self.max_rows is not None and rows > self.max_rows:
            rows = max(1, self.max_rows)
            cols = max(1, round(rows * src_width / (src_height * self.char_aspect)))
        return cols, rows

    @staticmethod
    def validate(frame: object) -> np.ndarray:
        """Check that *frame* is a usable image and return it as a uint8 array.

        Raises:
            InvalidFrameError: for ``None``, non-arrays, empty or oddly shaped arrays.
        """
        if frame is None:
            raise InvalidFrameError("Frame is None.")
        if not isinstance(frame, np.ndarray):
            raise InvalidFrameError(f"Frame must be a numpy array, got {type(frame).__name__}.")
        if frame.size == 0:
            raise InvalidFrameError("Frame is empty.")
        if frame.ndim not in (2, 3):
            raise InvalidFrameError(f"Unsupported frame dimensions: {frame.ndim}.")
        if frame.ndim == 3 and frame.shape[2] not in (1, 3, 4):
            raise InvalidFrameError(f"Unsupported channel count: {frame.shape[2]}.")
        if frame.dtype != np.uint8:
            if not (np.issubdtype(frame.dtype, np.integer) or np.issubdtype(frame.dtype, np.floating)):
                raise InvalidFrameError(f"Unsupported dtype: {frame.dtype}.")
            frame = np.clip(np.nan_to_num(frame), 0, 255).astype(np.uint8)
        return frame

    def _buffer(self, name: str, shape: tuple[int, ...]) -> np.ndarray | None:
        """Return the reusable buffer for *name* (resized when the grid changes), or ``None``."""
        if not self.reuse_buffers:
            return None
        current: np.ndarray | None = getattr(self, name)
        if current is None or current.shape != shape:
            current = np.empty(shape, np.uint8)
            setattr(self, name, current)
        return current

    def process(self, frame: np.ndarray) -> ProcessedFrame:
        """Resize *frame* to the character grid and compute its grayscale (and colour)."""
        img = self.validate(frame)
        if img.ndim == 3 and img.shape[2] == 1:
            img = img[:, :, 0]
        cols, rows = self.target_size(img.shape[1], img.shape[0])
        shrinking = cols <= img.shape[1] and rows <= img.shape[0]
        mode = _INTERPOLATION["area" if self.interpolation == "area" and shrinking else "linear"]
        channels = 1 if img.ndim == 2 else img.shape[2]
        # BGRA is reduced to BGR after the (smaller) resize.
        small = cv2.resize(img, (cols, rows), dst=self._buffer(
            "_small", (rows, cols) if channels == 1 else (rows, cols, channels)), interpolation=mode)
        if small.ndim == 2:
            color = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR) if self.keep_color else None
            return ProcessedFrame(small if self.reuse_buffers else np.ascontiguousarray(small), color)
        if small.shape[2] == 4:
            small = cv2.cvtColor(small, cv2.COLOR_BGRA2BGR)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY, dst=self._buffer("_gray", (rows, cols)))
        return ProcessedFrame(gray, small if self.keep_color else None)
