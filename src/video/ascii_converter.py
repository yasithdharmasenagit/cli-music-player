"""Brightness -> character mapping."""

from __future__ import annotations

import numpy as np

from src.core.config import CHARSET_HIGH
from src.video.frame_processor import FrameProcessor, ProcessedFrame

_RESET = "\x1b[0m"


class ASCIIConverter:
    """Converts OpenCV frames (BGR, BGRA or grayscale) into ASCII-art strings.

    Args:
        width: Output width in characters.
        charset: Characters ordered dark -> bright.
        invert: Swap the brightness mapping (for light-background terminals).
        color: Emit 24-bit ANSI foreground colours (quantised to 12 bits to keep output small).
        char_aspect: Terminal cell width/height ratio.
        max_rows: Optional cap on output rows.
    """

    def __init__(self, width: int = 120, charset: str = CHARSET_HIGH, invert: bool = False,
                 color: bool = False, char_aspect: float = 0.5,
                 max_rows: int | None = None) -> None:
        if len(charset) < 2:
            raise ValueError("charset needs at least 2 characters")
        self.charset = charset
        self.invert = invert
        self.color = color
        self.processor = FrameProcessor(width, char_aspect, keep_color=color, max_rows=max_rows)
        self._chars = np.array(list(charset), dtype="U1")
        self._lut = self._build_lut(len(charset), invert)

    @staticmethod
    def _build_lut(n_chars: int, invert: bool) -> np.ndarray:
        """256-entry table mapping brightness -> charset index."""
        idx = (np.arange(256, dtype=np.uint16) * n_chars // 256).astype(np.intp)
        return idx[::-1].copy() if invert else idx

    @property
    def width(self) -> int:
        """Configured maximum output width in characters."""
        return self.processor.width

    def output_size(self, src_width: int, src_height: int) -> tuple[int, int]:
        """``(cols, rows)`` that a frame of this pixel size will produce."""
        return self.processor.target_size(src_width, src_height)

    def convert(self, frame: np.ndarray) -> str:
        """Convert one frame to a newline-separated ASCII string.

        Raises:
            InvalidFrameError: if the frame is ``None``, empty or malformed.
        """
        return self.render(self.processor.process(frame))

    def render(self, processed: ProcessedFrame) -> str:
        """Render an already-processed frame."""
        grid = self._chars[self._lut[processed.gray]]  # (rows, cols) of 1-char strings
        if self.color and processed.color is not None:
            return self._render_color(grid, processed.color)
        rows, cols = grid.shape
        # A contiguous (rows, cols) 'U1' array viewed as 'U{cols}' yields one string per row.
        lines = np.ascontiguousarray(grid).view(f"U{cols}").reshape(rows)
        return "\n".join(lines.tolist())

    @staticmethod
    def _render_color(grid: np.ndarray, bgr: np.ndarray) -> str:
        q = bgr >> 4
        key = (q[:, :, 2].astype(np.uint16) << 8) | (q[:, :, 1].astype(np.uint16) << 4) | q[:, :, 0]
        out_lines: list[str] = []
        for row_keys, row_chars in zip(key, grid):
            changes = np.flatnonzero(np.r_[True, row_keys[1:] != row_keys[:-1]])
            ends = np.r_[changes[1:], len(row_keys)]
            parts: list[str] = []
            for start, end in zip(changes.tolist(), ends.tolist()):
                k = int(row_keys[start])
                r, g, b = (k >> 8) * 17, ((k >> 4) & 15) * 17, (k & 15) * 17
                parts.append(f"\x1b[38;2;{r};{g};{b}m")
                parts.append("".join(row_chars[start:end].tolist()))
            parts.append(_RESET)
            out_lines.append("".join(parts))
        return "\n".join(out_lines)
