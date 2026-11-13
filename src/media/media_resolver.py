"""Turn whatever the user typed (YouTube URL or file path) into playable sources."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.core.exceptions import (
    InvalidMediaPathError,
    InvalidURLError,
    UnsupportedMediaError,
)
from src.media.metadata import MediaKind, MediaMetadata, build_metadata
from src.media.youtube_resolver import YouTubeResolver, looks_like_url
from src.utils.logger import get_logger
from src.utils.paths import validate_media_path
from src.video.video_loader import VideoLoader

_log = get_logger("media_resolver")

VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".wmv", ".flv",
                              ".mpg", ".mpeg", ".ts", ".3gp"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac",
                              ".wma", ".aiff", ".aif", ".mka"})


@dataclass(frozen=True, slots=True)
class ResolvedMedia:
    """Everything the player needs to start.

    Attributes:
        metadata: Title, artist and duration.
        kind: Whether a video track is available.
        audio_source: What mpv plays (file path or direct stream URL).
        video_source: What OpenCV decodes for ASCII frames, or ``None`` (audio only).
        origin: Short human description (``"YouTube"`` / ``"Local file"``).
        user_agent: User-Agent mpv should use for network sources.
    """

    metadata: MediaMetadata
    kind: MediaKind
    audio_source: str
    video_source: str | None
    origin: str
    user_agent: str | None = None

    @property
    def has_video(self) -> bool:
        return self.video_source is not None


ProbeFn = Callable[[Path], MediaMetadata | None]


def probe_with_ffprobe(path: Path) -> MediaMetadata | None:
    """Read embedded title/artist/duration with ffprobe (part of FFmpeg), if available.

    Returns ``None`` when ffprobe is missing or fails; callers fall back to the filename.
    """
    exe = shutil.which("ffprobe")
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "quiet", "-print_format", "json", "-show_entries",
             "format=duration:format_tags=title,artist,TITLE,ARTIST", str(path)],
            capture_output=True, text=True, timeout=10, check=False)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    fmt = data.get("format") or {}
    tags = {str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
    try:
        duration = float(fmt["duration"]) if "duration" in fmt else None
    except (TypeError, ValueError):
        duration = None
    if not tags.get("title") and duration is None:
        return None
    return MediaMetadata(title=tags.get("title") or "", artist=tags.get("artist") or None,
                         duration=duration)


class MediaResolver:
    """Resolves user input into :class:`ResolvedMedia`.

    Args:
        youtube: YouTube resolver (injectable).
        probe: Local-file metadata probe (injectable; defaults to ffprobe).
    """

    def __init__(self, youtube: YouTubeResolver | None = None,
                 probe: ProbeFn = probe_with_ffprobe) -> None:
        self._youtube = youtube or YouTubeResolver()
        self._probe = probe

    def resolve(self, user_input: str) -> ResolvedMedia:
        """Resolve a URL or path.

        Raises:
            InvalidURLError, InvalidMediaPathError, UnsupportedMediaError,
            MediaUnavailableError, VideoLoadError.
        """
        text = user_input.strip().strip("\"'")
        if not text:
            raise InvalidMediaPathError("Nothing was entered.")
        if looks_like_url(text):
            return self._resolve_url(text)
        return self.resolve_local(text)

    def _resolve_url(self, url: str) -> ResolvedMedia:
        if not url.lower().startswith(("http://", "https://", "www.")) and "youtu" not in url.lower():
            raise InvalidURLError("That does not look like a supported URL.")
        media = self._youtube.resolve(url)
        s = media.streams
        return ResolvedMedia(
            metadata=media.metadata,
            kind=MediaKind.VIDEO if s.video_url else MediaKind.AUDIO,
            audio_source=s.audio_url,
            video_source=s.video_url,
            origin="YouTube",
            user_agent=s.headers.get("User-Agent"),
        )

    def resolve_local(self, raw_path: str | Path) -> ResolvedMedia:
        """Resolve a local audio or video file."""
        path = validate_media_path(raw_path)
        ext = path.suffix.lower()
        if ext not in VIDEO_EXTENSIONS and ext not in AUDIO_EXTENSIONS:
            raise UnsupportedMediaError(
                f"Unsupported file type '{ext or path.name}'. "
                f"Supported: {', '.join(sorted(VIDEO_EXTENSIONS | AUDIO_EXTENSIONS))}")
        probed = self._probe(path)
        duration = probed.duration if probed else None
        is_video = ext in VIDEO_EXTENSIONS
        if is_video:
            # Validate decodability now so errors show before the player starts.
            with VideoLoader(path) as loader:
                if duration is None and loader.info.duration:
                    duration = loader.info.duration
        stem = path.stem.replace("_", " ")
        meta = build_metadata(
            (probed.title if probed and probed.title else None) or stem,
            artist=probed.artist if probed else None,
            duration=duration,
            fallback_title=path.stem,
        )
        _log.info("Resolved local %s file %s", "video" if is_video else "audio", path.name)
        return ResolvedMedia(
            metadata=meta,
            kind=MediaKind.VIDEO if is_video else MediaKind.AUDIO,
            audio_source=str(path),
            video_source=str(path) if is_video else None,
            origin="Local file",
        )
