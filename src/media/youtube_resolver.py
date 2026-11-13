"""YouTube URL validation and permitted stream resolution via yt-dlp.

Only publicly accessible media is resolved. No cookies, logins or DRM/age-gate workarounds
are used, and stream URLs (which embed access tokens) are never logged.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.core.exceptions import InvalidURLError, MediaUnavailableError
from src.media.metadata import MediaMetadata, build_metadata
from src.utils.logger import get_logger

_log = get_logger("youtube")

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
             "youtube-nocookie.com", "www.youtube-nocookie.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_ID_PREFIXES = ("shorts", "embed", "live", "v")

#: Preferred height for the ASCII video stream: more pixels are wasted on a terminal grid.
PREFERRED_VIDEO_HEIGHT = 360
MAX_VIDEO_HEIGHT = 720


def looks_like_url(text: str) -> bool:
    """Whether *text* is an http(s) URL or something a user would mean as one."""
    t = text.strip().lower()
    return t.startswith(("http://", "https://", "www.")) or "youtube.com/" in t or "youtu.be/" in t


def extract_video_id(url: str) -> str:
    """Return the 11-character video id from a YouTube URL.

    Raises:
        InvalidURLError: if the URL is not a valid single-video YouTube URL.
    """
    text = url.strip()
    if not text:
        raise InvalidURLError("The URL is empty.")
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise InvalidURLError("That does not look like a valid URL.") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise InvalidURLError("That does not look like a valid URL.")
    video_id: str | None = None
    if host in _SHORT_HOSTS:
        video_id = parsed.path.strip("/").split("/")[0]
    elif host in _YT_HOSTS:
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path.rstrip("/") == "/watch":
            video_id = (parse_qs(parsed.query).get("v") or [""])[0]
        elif len(parts) >= 2 and parts[0] in _PATH_ID_PREFIXES:
            video_id = parts[1]
        elif parts and parts[0] in ("playlist", "channel", "c", "user", "feed", "results"):
            raise InvalidURLError("That is not a single-video link. Paste a link to one video.")
    else:
        raise InvalidURLError(
            f"Unsupported site: {host}. Paste a YouTube link or a local file path.")
    if not video_id or not _ID_RE.match(video_id):
        raise InvalidURLError("Could not find a valid YouTube video id in that URL.")
    return video_id


def is_youtube_url(url: str) -> bool:
    """True if *url* is a valid single-video YouTube URL."""
    try:
        extract_video_id(url)
    except InvalidURLError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class StreamSelection:
    """Chosen direct stream URLs. ``video_url`` is ``None`` for audio-only results."""

    audio_url: str
    video_url: str | None
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class YouTubeMedia:
    """Result of resolving a YouTube URL."""

    video_id: str
    metadata: MediaMetadata
    streams: StreamSelection


def _is_playable(fmt: dict[str, Any]) -> bool:
    if not fmt.get("url"):
        return False
    if fmt.get("has_drm") or "drm" in str(fmt.get("format_note", "")).lower():
        return False
    proto = str(fmt.get("protocol", "https"))
    return proto in ("https", "http", "m3u8", "m3u8_native") and not proto.startswith("dash_frag")


def select_streams(formats: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Pick ``(audio_url, video_url)`` from yt-dlp's format list.

    Audio: best audio-only format by bitrate (falls back to any format with audio).
    Video: a video track near :data:`PREFERRED_VIDEO_HEIGHT` (never above
    :data:`MAX_VIDEO_HEIGHT`), preferring progressive https formats.
    """
    playable = [f for f in formats if _is_playable(f)]
    none = ("none", None)
    audio_only = [f for f in playable if f.get("acodec") not in none and f.get("vcodec") in none]
    with_audio = [f for f in playable if f.get("acodec") not in none]
    audio_pool = audio_only or with_audio
    audio = max(audio_pool, key=lambda f: float(f.get("abr") or f.get("tbr") or 0), default=None)

    videos = [f for f in playable if f.get("vcodec") not in none and f.get("height")
              and int(f["height"]) <= MAX_VIDEO_HEIGHT]

    def video_rank(f: dict[str, Any]) -> tuple[int, int, int]:
        h = int(f["height"])
        protocol_pref = 0 if str(f.get("protocol")) in ("https", "http") else 1
        # Prefer h264/avc (decodable by every OpenCV build), then closeness to target height.
        codec_pref = 0 if str(f.get("vcodec", "")).startswith(("avc1", "h264")) else 1
        return (protocol_pref, codec_pref, abs(h - PREFERRED_VIDEO_HEIGHT))

    video = min(videos, key=video_rank, default=None)
    return (audio["url"] if audio else None, video["url"] if video else None)


Extractor = Callable[[str], dict[str, Any]]


def _default_extractor(url: str) -> dict[str, Any]:
    import yt_dlp  # imported lazily: heavy, and optional for local-only use

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 15,
        "retries": 2,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return dict(info or {})


class YouTubeResolver:
    """Resolves metadata and permitted direct stream URLs for a YouTube link.

    Args:
        extractor: Callable returning a yt-dlp style info dict (injectable for tests).
    """

    def __init__(self, extractor: Extractor | None = None) -> None:
        self._extract = extractor or _default_extractor

    def resolve(self, url: str) -> YouTubeMedia:
        """Validate *url* and fetch metadata and streams.

        Raises:
            InvalidURLError: malformed / unsupported URL.
            MediaUnavailableError: video private, region/age restricted, removed, live, DRM,
                offline, or no accessible streams.
        """
        video_id = extract_video_id(url)
        canonical = f"https://www.youtube.com/watch?v={video_id}"
        try:
            info = self._extract(canonical)
        except ImportError as exc:
            raise MediaUnavailableError(
                "yt-dlp is not installed. Run: pip install yt-dlp") from exc
        except Exception as exc:  # yt_dlp.utils.DownloadError and network errors
            raise MediaUnavailableError(_explain_extractor_error(exc)) from exc
        if not info:
            raise MediaUnavailableError("YouTube returned no information for this video.")
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise MediaUnavailableError("Live streams and premieres are not supported.")
        metadata = build_metadata(
            info.get("track") or info.get("title"),
            channel=info.get("channel") or info.get("uploader"),
            artist=info.get("artist") or _first(info.get("artists")),
            duration=_as_float(info.get("duration")),
        )
        audio_url, video_url = select_streams(list(info.get("formats") or [])) \
            if info.get("formats") else (info.get("url"), None)
        if not audio_url:
            raise MediaUnavailableError(
                "No accessible audio stream is available for this video "
                "(it may be restricted or DRM-protected).")
        headers = {str(k): str(v) for k, v in (info.get("http_headers") or {}).items()
                   if str(k).lower() in ("user-agent", "referer", "accept-language")}
        _log.info("Resolved YouTube video %s (video stream: %s)", video_id, bool(video_url))
        return YouTubeMedia(video_id, metadata, StreamSelection(audio_url, video_url, headers))


def _first(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _explain_extractor_error(exc: Exception) -> str:
    """Translate extractor errors into short user-facing text (no URLs/tokens)."""
    text = str(exc).lower()
    if "private video" in text:
        return "This video is private."
    if "sign in" in text or "age" in text and "restrict" in text or "confirm your age" in text:
        return "This video requires sign-in or age verification, which is not supported."
    if "not available in your country" in text or "geo" in text:
        return "This video is not available in your region."
    if "removed" in text or "unavailable" in text or "does not exist" in text:
        return "This video is unavailable."
    if "drm" in text:
        return "This video is DRM-protected and cannot be played."
    if any(w in text for w in ("urlopen", "timed out", "timeout", "connection", "network",
                               "getaddrinfo", "resolve")):
        return "Network problem while contacting YouTube. Check your connection."
    return "YouTube did not provide an accessible stream for this video."
