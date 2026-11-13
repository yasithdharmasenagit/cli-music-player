"""Pick the lyric lines to display for a playback position."""

from __future__ import annotations

import threading
from bisect import bisect_right
from collections.abc import Callable, Sequence

from src.lyrics.lyrics_models import LyricLine, Lyrics, LyricsStatus, LyricsWindow


class LyricsSynchronizer:
    """Maps playback time to lyric lines.

    Lines with identical timestamps form one *group* and are shown together (for example a
    line and its translation). Empty-text groups are gaps: they clear the current lyric.

    Args:
        lines: Time-sorted lyric lines.
        context: How many groups before/after the current one to return.
    """

    def __init__(self, lines: Sequence[LyricLine], context: int = 1) -> None:
        self._context = max(0, context)
        times: list[float] = []
        groups: list[list[str]] = []
        for line in lines:
            if times and line.time == times[-1]:
                groups[-1].append(line.text)
            else:
                times.append(line.time)
                groups.append([line.text])
        self._times = times
        self._groups = [tuple(t for t in g if t) for g in groups]

    def __len__(self) -> int:
        return len(self._times)

    def index_at(self, position: float) -> int:
        """Index of the group active at *position*, or ``-1`` before the first lyric."""
        return bisect_right(self._times, position) - 1

    def window_at(self, position: float) -> LyricsWindow:
        """Previous / current / upcoming text for *position*."""
        idx = self.index_at(position)
        return LyricsWindow(
            previous=self._neighbours(range(idx - self._context, idx)),
            current=self._groups[idx] if idx >= 0 else (),
            upcoming=self._neighbours(range(idx + 1, idx + 1 + self._context)),
            index=idx,
        )

    def _neighbours(self, indices: range) -> tuple[str, ...]:
        out: list[str] = []
        for i in indices:
            if 0 <= i < len(self._groups):
                out.extend(self._groups[i])
        return tuple(out)


class LyricsTrack:
    """Fetches lyrics on a background thread so playback never waits for the network.

    Until the lookup finishes, :attr:`message` reads "Searching for lyrics…" and
    :meth:`window_at` returns an empty window.
    """

    def __init__(self, fetch: Callable[[], Lyrics] | None) -> None:
        self._lock = threading.Lock()
        self._lyrics: Lyrics | None = None
        self._sync: LyricsSynchronizer | None = None
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        if fetch is None:
            self._set(Lyrics(LyricsStatus.DISABLED))
        else:
            self._thread = threading.Thread(target=self._run, args=(fetch,),
                                            name="lyrics-fetch", daemon=True)

    def start(self) -> None:
        """Begin the lookup (no-op when lyrics are disabled)."""
        if self._thread is not None and not self._thread.is_alive() and not self._done.is_set():
            self._thread.start()

    def _run(self, fetch: Callable[[], Lyrics]) -> None:
        try:
            result = fetch()
        except Exception:  # lyrics must never take playback down
            result = Lyrics(LyricsStatus.ERROR, message="Lyrics lookup failed.")
        self._set(result)

    def _set(self, lyrics: Lyrics) -> None:
        with self._lock:
            self._lyrics = lyrics
            self._sync = LyricsSynchronizer(lyrics.lines) if lyrics.is_synced else None
        self._done.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the lookup finished (used by tests)."""
        return self._done.wait(timeout)

    @property
    def ready(self) -> bool:
        return self._done.is_set()

    @property
    def lyrics(self) -> Lyrics | None:
        with self._lock:
            return self._lyrics

    @property
    def is_synced(self) -> bool:
        with self._lock:
            return self._sync is not None

    @property
    def message(self) -> str:
        """Text to show when there are no synchronized lines to display."""
        with self._lock:
            if self._lyrics is None:
                return "Searching for lyrics…"
            return "" if self._sync is not None else self._lyrics.status_text

    def window_at(self, position: float) -> LyricsWindow:
        with self._lock:
            sync = self._sync
        return sync.window_at(position) if sync is not None else LyricsWindow()

    def index_at(self, position: float) -> int:
        with self._lock:
            sync = self._sync
        return sync.index_at(position) if sync is not None else -1
