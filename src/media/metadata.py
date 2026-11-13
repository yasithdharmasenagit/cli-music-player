"""Media metadata model and title/artist normalisation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class MediaKind(Enum):
    """What the resolved source contains."""

    VIDEO = "video"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    """Descriptive information about a track.

    Attributes:
        title: Track title (cleaned for display and lyrics search).
        artist: Artist or channel name, if known.
        duration: Length in seconds, if known.
    """

    title: str
    artist: str | None = None
    duration: float | None = None

    @property
    def display(self) -> str:
        """``Artist - Title`` (or just the title)."""
        return f"{self.artist} - {self.title}" if self.artist else self.title


# "(Official Video)", "[Lyrics]", "(Audio)", "(HD)", "(Remastered 2011)", "| Official ..." etc.
_NOISE = re.compile(
    r"[\(\[\{]\s*(?:official|lyrics?|lyric video|audio|video|music video|hd|hq|4k|remaster(?:ed)?|"
    r"visuali[sz]er|live|clip officiel)[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)
_FEAT_TAIL = re.compile(r"\s*[\(\[]\s*(?:feat|ft)\.?[^\)\]]*[\)\]]", re.IGNORECASE)
_CHANNEL_SUFFIX = re.compile(r"\s*(?:-\s*Topic|VEVO|Official(?:\s+Channel)?)\s*$", re.IGNORECASE)
_SEPARATORS = (" - ", " – ", " — ", " ‐ ")


def clean_title(title: str) -> str:
    """Strip decorations like ``(Official Video)`` and collapse whitespace."""
    cleaned = _NOISE.sub("", title)
    cleaned = re.sub(r"\s*\|.*$", "", cleaned)  # "Song | Official Video"
    return re.sub(r"\s+", " ", cleaned).strip(" -–—_")


def clean_artist(name: str) -> str:
    """Strip ``- Topic`` / ``VEVO`` style channel suffixes."""
    return _CHANNEL_SUFFIX.sub("", name).strip()


def split_artist_title(raw_title: str) -> tuple[str | None, str]:
    """Split ``"Artist - Title"`` into its parts; ``(None, title)`` when there is no separator."""
    for sep in _SEPARATORS:
        if sep in raw_title:
            artist, _, title = raw_title.partition(sep)
            artist, title = artist.strip(), title.strip()
            if artist and title:
                return artist, title
    return None, raw_title.strip()


def build_metadata(raw_title: str | None, channel: str | None = None,
                   artist: str | None = None, duration: float | None = None,
                   fallback_title: str = "Unknown title") -> MediaMetadata:
    """Combine whatever the source provides into clean :class:`MediaMetadata`.

    Preference order for the artist: explicit *artist* field, ``"Artist - Title"`` split of
    the title, then the (cleaned) channel name.
    """
    title_text = (raw_title or "").strip()
    split_artist, title_only = split_artist_title(title_text) if title_text else (None, "")
    title = clean_title(title_only if split_artist else title_text) or fallback_title
    chosen = (clean_artist(artist) if artist else None) or (
        clean_title(split_artist) if split_artist else None) or (
        clean_artist(channel) if channel else None)
    # Remove a trailing "feat." from the title only for lookups elsewhere; keep it for display.
    if duration is not None and duration <= 0:
        duration = None
    return MediaMetadata(title=title, artist=chosen or None, duration=duration)


_GENERIC_TITLE = re.compile(
    r"^(videoplayback|video|audio|untitled|download|output|movie|clip|recording|"
    r"(?:vid|img|mov|clip|video|audio|track|screen ?recording)[ _-]?\d*|\d+|unknown title)$",
    re.IGNORECASE)


def is_generic_title(title: str) -> bool:
    """True for names that identify no track (``videoplayback``, ``VID_0012``, ``video``...)."""
    return not title.strip() or bool(_GENERIC_TITLE.match(title.strip()))


def lookup_title(title: str) -> str:
    """Title variant used for lyrics search (drops ``(feat. X)``)."""
    return _FEAT_TAIL.sub("", title).strip()
