"""Brightness -> character mapping."""

from __future__ import annotations

import cv2
import numpy as np

from src.core.config import CHARSET_HIGH
from src.video.frame_processor import FrameProcessor, ProcessedFrame

_RESET = "\x1b[0m"
_NEWLINE = 10


class ASCIIConverter:
    """Converts OpenCV frames (BGR, BGRA or grayscale) into ASCII-art strings.

    For plain-ASCII character sets (the default ramps) the mapping runs entirely on bytes:
    one lookup into a reused ``(rows, cols+1)`` buffer and a single decode, with no
    per-character Python objects. Other character sets (for example block characters) use a
    slower, equivalent Unicode path.

    Args:
        width: Output width in characters.
        charset: Characters ordered dark -> bright.
        invert: Swap the brightness mapping (for light-background terminals).
        color: Emit 24-bit ANSI foreground colours (quantised to 12 bits to keep output small).
        char_aspect: Terminal cell width/height ratio.
        max_rows: Optional cap on output rows.
        interpolation: ``"area"`` or ``"linear"`` (see :class:`FrameProcessor`).
        stabilize: Suppress shimmer. Video compression noise moves pixel brightness by a few
            levels from frame to frame; mapped to characters this makes still areas flicker
            ("old TV"). With this on, a cell only changes when its brightness has moved by about
            one step of the character ramp since the cell was last updated, so noise is ignored
            while real motion and fades still show.
        chunk: ``1`` = every terminal cell is a sample; ``2`` = sample at half resolution and
            draw each sample as a 2x2 block (chunky and cheap; the output size is unchanged).
        enhance: ``"none"``, ``"levels"`` (smoothly stretch the brightness range so details use
            the whole ramp) or ``"detail"`` (levels plus local contrast and sharpening).
    """

    def __init__(self, width: int = 120, charset: str = CHARSET_HIGH, invert: bool = False,
                 color: bool = False, char_aspect: float = 0.5,
                 max_rows: int | None = None, interpolation: str = "area",
                 stabilize: bool = False, chunk: int = 1, enhance: str = "none") -> None:
        if len(charset) < 2:
            raise ValueError("charset needs at least 2 characters")
        if chunk < 1:
            raise ValueError("chunk must be >= 1")
        if enhance not in ("none", "levels", "detail"):
            raise ValueError("enhance must be 'none', 'levels' or 'detail'")
        self.chunk = chunk
        self.enhance = enhance
        self.charset = charset
        self.invert = invert
        self.color = color
        # The converter consumes each processed frame immediately, so buffers can be reused.
        # With chunk > 1 the video is sampled on a coarser grid that is blown up to the full size.
        self.processor = FrameProcessor(
            max(1, width // chunk), char_aspect, keep_color=color,
            max_rows=None if max_rows is None else max(1, max_rows // chunk),
            interpolation=interpolation, reuse_buffers=True)
        self._lut = self._build_lut(len(charset), invert)
        self._ascii = charset.isascii()
        self._byte_table = np.array([ord(charset[i]) for i in self._lut], np.uint8) \
            if self._ascii else None
        self._chars = np.array(list(charset), dtype="U1")
        self._out: np.ndarray | None = None  # (rows, cols + 1) bytes, last column is "\n"
        self._mapped: np.ndarray | None = None  # (rows, cols) lookup result, reused
        self.stabilize = stabilize
        #: Brightness change (0-255) a cell needs before its character may change.
        self.stability_threshold = max(8, round(256 / len(charset)))
        self._shown_gray: np.ndarray | None = None   # brightness each cell last displayed
        self._stable_gray: np.ndarray | None = None
        self._keep: np.ndarray | None = None
        self._shown_color: np.ndarray | None = None
        self._stable_color: np.ndarray | None = None
        # detail enhancement state
        self._level_lo: float | None = None
        self._level_hi: float | None = None
        self._enhanced: np.ndarray | None = None
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 4)) if enhance == "detail" else None
        self._blur: np.ndarray | None = None
        # chunk (block upscaling) buffers
        self._big_gray: np.ndarray | None = None
        self._big_color: np.ndarray | None = None

    @staticmethod
    def _build_lut(n_chars: int, invert: bool) -> np.ndarray:
        """256-entry table mapping brightness -> charset index."""
        idx = (np.arange(256, dtype=np.uint16) * n_chars // 256).astype(np.intp)
        return idx[::-1].copy() if invert else idx

    @property
    def width(self) -> int:
        """Maximum output width in characters."""
        return self.processor.width * self.chunk

    def output_size(self, src_width: int, src_height: int) -> tuple[int, int]:
        """``(cols, rows)`` that a frame of this pixel size will produce."""
        cols, rows = self.processor.target_size(src_width, src_height)
        return cols * self.chunk, rows * self.chunk

    def convert(self, frame: np.ndarray) -> str:
        """Convert one frame to a newline-separated ASCII string.

        Raises:
            InvalidFrameError: if the frame is ``None``, empty or malformed.
        """
        return self.render(self.processor.process(frame))

    def _stabilized(self, processed: ProcessedFrame) -> ProcessedFrame:
        """Pin cells whose brightness has not moved by ``stability_threshold`` since last shown."""
        gray = processed.gray
        shown = self._shown_gray
        if shown is None or shown.shape != gray.shape:
            self._shown_gray = gray.copy()
            self._stable_gray = np.empty_like(gray)
            self._keep = np.empty_like(gray)
            self._shown_color = processed.color.copy() if processed.color is not None else None
            self._stable_color = np.empty_like(processed.color) if processed.color is not None else None
            return processed
        assert self._stable_gray is not None and self._keep is not None
        diff = cv2.absdiff(gray, shown)
        # (OpenCV accepts a scalar here; its type stubs only list arrays.)
        cv2.compare(diff, self.stability_threshold, cv2.CMP_LT, dst=self._keep)  # type: ignore[call-overload]
        np.copyto(self._stable_gray, gray)
        cv2.copyTo(shown, self._keep, self._stable_gray)  # unchanged cells keep what was shown
        np.copyto(shown, self._stable_gray)
        color = processed.color
        if color is not None and self._shown_color is not None and self._stable_color is not None:
            np.copyto(self._stable_color, color)
            cv2.copyTo(self._shown_color, self._keep, self._stable_color)
            np.copyto(self._shown_color, self._stable_color)
            color = self._stable_color
        return ProcessedFrame(self._stable_gray, color)

    def _enhanced_frame(self, processed: ProcessedFrame) -> ProcessedFrame:
        """Stretch the brightness range (smoothly, so the picture does not pump) and, for
        ``"detail"``, add local contrast and a little sharpening."""
        gray = processed.gray
        if self._enhanced is None or self._enhanced.shape != gray.shape:
            self._enhanced = np.empty_like(gray)
            self._blur = np.empty_like(gray)
        lo, hi = (float(v) for v in np.percentile(gray, (2, 98)))
        if self._level_lo is None or self._level_hi is None:
            self._level_lo, self._level_hi = lo, hi
        else:
            # Follow slow drifts gently, but jump on a scene cut.
            fast = abs(lo - self._level_lo) > 40 or abs(hi - self._level_hi) > 40
            alpha = 0.5 if fast else 0.08
            self._level_lo += alpha * (lo - self._level_lo)
            self._level_hi += alpha * (hi - self._level_hi)
        span = max(self._level_hi - self._level_lo, 48.0)  # never amplify near-flat pictures wildly
        table = np.clip((np.arange(256) - self._level_lo) * (255.0 / span), 0, 255).astype(np.uint8)
        cv2.LUT(gray, table, dst=self._enhanced)
        if self._clahe is not None and self._blur is not None:
            self._clahe.apply(self._enhanced, self._enhanced)
            cv2.GaussianBlur(self._enhanced, (0, 0), 1.0, dst=self._blur)
            cv2.addWeighted(self._enhanced, 1.4, self._blur, -0.4, 0, dst=self._enhanced)
        return ProcessedFrame(self._enhanced, processed.color)

    def _chunked(self, processed: ProcessedFrame) -> ProcessedFrame:
        """Blow the sample grid up to full size: each sample becomes a chunk x chunk block."""
        rows, cols = processed.gray.shape
        size = (cols * self.chunk, rows * self.chunk)
        if self._big_gray is None or self._big_gray.shape != (size[1], size[0]):
            self._big_gray = np.empty((size[1], size[0]), np.uint8)
            self._big_color = np.empty((size[1], size[0], 3), np.uint8)                 if processed.color is not None else None
        cv2.resize(processed.gray, size, dst=self._big_gray, interpolation=cv2.INTER_NEAREST)
        color = None
        if processed.color is not None and self._big_color is not None:
            cv2.resize(processed.color, size, dst=self._big_color, interpolation=cv2.INTER_NEAREST)
            color = self._big_color
        return ProcessedFrame(self._big_gray, color)

    def render(self, processed: ProcessedFrame) -> str:
        """Render an already-processed frame."""
        if self.enhance != "none":
            processed = self._enhanced_frame(processed)
        if self.stabilize:
            processed = self._stabilized(processed)
        if self.chunk > 1:
            processed = self._chunked(processed)
        if self.color and processed.color is not None:
            return self._render_color(self._chars[self._lut[processed.gray]], processed.color)
        if self._byte_table is not None:
            return self._render_bytes(processed.gray)
        grid = self._chars[self._lut[processed.gray]]  # (rows, cols) of 1-char strings
        rows, cols = grid.shape
        # A contiguous (rows, cols) 'U1' array viewed as 'U{cols}' yields one string per row.
        lines = np.ascontiguousarray(grid).view(f"U{cols}").reshape(rows)
        return "\n".join(lines.tolist())

    def _render_bytes(self, gray: np.ndarray) -> str:
        assert self._byte_table is not None
        rows, cols = gray.shape
        out, mapped = self._out, self._mapped
        if out is None or mapped is None or out.shape != (rows, cols + 1):
            out = np.empty((rows, cols + 1), np.uint8)
            out[:, cols] = _NEWLINE
            mapped = np.empty((rows, cols), np.uint8)
            self._out, self._mapped = out, mapped
        # cv2.LUT maps uint8 -> uint8 directly (no 8-byte index array like fancy indexing/take).
        cv2.LUT(np.ascontiguousarray(gray), self._byte_table, dst=mapped)
        out[:, :cols] = mapped
        # Drop the final newline; decode straight from the buffer (no intermediate bytes copy).
        return str(memoryview(out.reshape(-1))[:-1], "ascii")

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
