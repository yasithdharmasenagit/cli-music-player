"""Lyrics data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LyricsStatus(Enum):
    """Outcome of a lyrics lookup."""

    SYNCED = "synced"            # timestamped lines available
    UNSYNCED = "unsynced"        # only plain text exists (never shown as synced)
    INSTRUMENTAL = "instrumental"
    NOT_FOUND = "not_found"
    ERROR = "error"              # network failure, timeout, bad response
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class LyricLine:
    """One timestamped line. ``text`` may be empty (an instrumental gap)."""

    time: float
    text: str


@dataclass(frozen=True, slots=True)
class Lyrics:
    """Result of a lyrics lookup."""

    status: LyricsStatus
    lines: tuple[LyricLine, ...] = field(default_factory=tuple)
    source: str = ""
    message: str = ""

    @property
    def is_synced(self) -> bool:
        return self.status is LyricsStatus.SYNCED and bool(self.lines)

    @property
    def status_text(self) -> str:
        """Short, user-facing explanation when there is nothing to display."""
        if self.message:
            return self.message
        return {
            LyricsStatus.SYNCED: "",
            LyricsStatus.UNSYNCED: "Lyrics exist but are not time-synchronized.",
            LyricsStatus.INSTRUMENTAL: "Instrumental",
            LyricsStatus.NOT_FOUND: "No lyrics found.",
            LyricsStatus.ERROR: "Lyrics unavailable (network problem).",
            LyricsStatus.DISABLED: "Lyrics disabled.",
        }[self.status]


@dataclass(frozen=True, slots=True)
class LyricsWindow:
    """What to show at a given moment: previous, current and upcoming line groups."""

    previous: tuple[str, ...] = ()
    current: tuple[str, ...] = ()
    upcoming: tuple[str, ...] = ()
    index: int = -1  # index of the current timestamp group; -1 = before the first lyric
