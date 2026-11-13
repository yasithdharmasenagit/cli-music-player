"""Non-blocking, Enter-less keyboard input (Windows msvcrt; POSIX termios fallback)."""

from __future__ import annotations

import os
import sys
from enum import Enum
from types import TracebackType
from typing import Protocol


class Key(Enum):
    """Logical player commands."""

    PAUSE = "pause"
    QUIT = "quit"
    SEEK_BACK = "seek_back"
    SEEK_FORWARD = "seek_forward"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    TOGGLE_LYRICS = "toggle_lyrics"
    TOGGLE_VIDEO = "toggle_video"


_PLAIN = {
    " ": Key.PAUSE,
    "q": Key.QUIT, "Q": Key.QUIT, "\x03": Key.QUIT,  # Ctrl+C arrives as a character in raw mode
    "l": Key.TOGGLE_LYRICS, "L": Key.TOGGLE_LYRICS,
    "v": Key.TOGGLE_VIDEO, "V": Key.TOGGLE_VIDEO,
}
# Windows extended-key codes, sent after a '\x00' or '\xe0' prefix.
_WINDOWS_EXT = {"H": Key.VOLUME_UP, "P": Key.VOLUME_DOWN, "K": Key.SEEK_BACK, "M": Key.SEEK_FORWARD}
# POSIX ANSI arrow final bytes after ESC [ (or ESC O).
_ANSI_ARROWS = {"A": Key.VOLUME_UP, "B": Key.VOLUME_DOWN, "D": Key.SEEK_BACK, "C": Key.SEEK_FORWARD}


def decode_windows(first: str, second: str | None = None) -> Key | None:
    """Translate msvcrt character(s) into a :class:`Key`."""
    if first in ("\x00", "\xe0"):
        return _WINDOWS_EXT.get(second or "")
    return _PLAIN.get(first)


def decode_ansi(sequence: str) -> Key | None:
    """Translate a POSIX character or escape sequence into a :class:`Key`."""
    if len(sequence) == 3 and sequence[0] == "\x1b" and sequence[1] in "[O":
        return _ANSI_ARROWS.get(sequence[2])
    if len(sequence) == 1:
        return _PLAIN.get(sequence)
    return None


class CharSource(Protocol):
    """Low-level character source (replaceable in tests)."""

    def has_char(self) -> bool: ...
    def get_char(self) -> str: ...


class WindowsCharSource:
    """msvcrt-based reader."""

    def __init__(self) -> None:
        import msvcrt
        self._m = msvcrt

    def has_char(self) -> bool:
        return bool(self._m.kbhit())  # type: ignore[attr-defined]

    def get_char(self) -> str:
        return str(self._m.getwch())  # type: ignore[attr-defined]


class PosixCharSource:
    """cbreak-mode stdin reader (terminal mode is restored by :meth:`restore`)."""

    def __init__(self) -> None:
        import select
        import termios
        import tty
        self._select, self._termios = select, termios
        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)  # type: ignore[attr-defined]
        tty.setcbreak(self._fd)  # type: ignore[attr-defined]

    def has_char(self) -> bool:
        return bool(self._select.select([sys.stdin], [], [], 0)[0])

    def get_char(self) -> str:
        ch = os.read(self._fd, 1).decode("utf-8", errors="ignore")
        if ch == "\x1b" and self.has_char():
            ch += os.read(self._fd, 2).decode("utf-8", errors="ignore")
        return ch

    def restore(self) -> None:
        self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._saved)  # type: ignore[attr-defined]


class KeyReader:
    """Polls for key presses without blocking. Use as a context manager.

    Args:
        source: Character source; defaults to the platform implementation (only when stdin
            is an interactive terminal, so piped runs simply receive no keys).
        windows: Force Windows or POSIX decoding (for tests).
    """

    def __init__(self, source: CharSource | None = None, windows: bool | None = None) -> None:
        self._source = source
        self._windows = (sys.platform == "win32") if windows is None else windows

    def __enter__(self) -> KeyReader:
        if self._source is None and sys.stdin is not None and sys.stdin.isatty():
            self._source = WindowsCharSource() if self._windows else PosixCharSource()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        restore = getattr(self._source, "restore", None)
        if restore is not None:
            restore()

    def poll(self, limit: int = 16) -> list[Key]:
        """Return all commands pressed since the last call (at most *limit* characters)."""
        keys: list[Key] = []
        src = self._source
        if src is None:
            return keys
        for _ in range(limit):
            if not src.has_char():
                break
            ch = src.get_char()
            if self._windows and ch in ("\x00", "\xe0"):
                key = decode_windows(ch, src.get_char() if src.has_char() else None)
            elif self._windows:
                key = decode_windows(ch)
            else:
                key = decode_ansi(ch)
            if key is not None:
                keys.append(key)
        return keys
