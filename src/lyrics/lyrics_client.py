"""Lyrics lookup: LRCLIB provider, local cache, and a provider chain."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from src.lyrics.lyrics_models import LyricLine, Lyrics, LyricsStatus
from src.lyrics.lyrics_parser import parse_lrc
from src.media.metadata import MediaMetadata, is_generic_title, lookup_title
from src.utils.logger import get_logger

_log = get_logger("lyrics")

USER_AGENT = "ascii-music-player/0.2.0"
#: Candidate durations further than this from the track's are treated as a different recording.
DURATION_TOLERANCE = 6.0


@dataclass(frozen=True, slots=True)
class LyricsQuery:
    """Search key derived from track metadata."""

    title: str
    artist: str | None = None
    duration: float | None = None

    @classmethod
    def from_metadata(cls, meta: MediaMetadata) -> LyricsQuery:
        return cls(lookup_title(meta.title), meta.artist, meta.duration)

    @property
    def cache_key(self) -> str:
        raw = f"{(self.artist or '').strip().lower()}|{self.title.strip().lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class LyricsProvider(Protocol):
    """A source of lyrics. Implementations must never raise for expected failures."""

    name: str

    def fetch(self, query: LyricsQuery) -> Lyrics: ...


class LrclibProvider:
    """Client for the public LRCLIB API (https://lrclib.net), which offers synced lyrics.

    Args:
        base_url: API root, e.g. ``https://lrclib.net/api``.
        timeout: Seconds before a request is abandoned.
        client: Optional preconfigured ``httpx.Client`` (used by tests).
    """

    name = "LRCLIB"

    def __init__(self, base_url: str = "https://lrclib.net/api", timeout: float = 6.0,
                 client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        # LRCLIB can be slow (search sometimes takes 15 s+); the lookup runs in the background,
        # so a generous read timeout costs nothing, while connecting must still fail fast.
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            headers={"User-Agent": USER_AGENT})
        self._owns_client = client is None

    def close(self) -> None:
        """Close the HTTP client if this provider created it."""
        if self._owns_client:
            self._client.close()

    def fetch(self, query: LyricsQuery) -> Lyrics:
        """Look up lyrics, preferring synchronized ones. Never raises."""
        try:
            candidates = self._candidates(query)
        except httpx.TimeoutException:
            return Lyrics(LyricsStatus.ERROR, source=self.name,
                          message="Lyrics service is slow or unreachable (timed out).")
        except (httpx.HTTPError, ValueError):
            return Lyrics(LyricsStatus.ERROR, source=self.name,
                          message="Lyrics unavailable (network or service problem).")
        return self._choose(candidates, query)

    # -- requests

    def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        response = self._client.get(f"{self._base}/{path}", params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _candidates(self, query: LyricsQuery) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if query.artist and query.duration:
            exact = self._get_json("get", {"track_name": query.title,
                                           "artist_name": query.artist,
                                           "duration": int(round(query.duration))})
            if isinstance(exact, dict):
                found.append(exact)
                if self._usable(exact, query):
                    return found
        params: dict[str, Any] = {"track_name": query.title}
        if query.artist:
            params["artist_name"] = query.artist
        results = self._get_json("search", params)
        if isinstance(results, list):
            found.extend(r for r in results if isinstance(r, dict))
        return found

    # -- selection

    @staticmethod
    def _duration_ok(item: dict[str, Any], query: LyricsQuery) -> bool:
        if query.duration is None or item.get("duration") is None:
            return True
        try:
            return abs(float(item["duration"]) - query.duration) <= DURATION_TOLERANCE
        except (TypeError, ValueError):
            return True

    def _usable(self, item: dict[str, Any], query: LyricsQuery) -> bool:
        return bool(item.get("syncedLyrics")) and self._duration_ok(item, query)

    def _choose(self, candidates: list[dict[str, Any]], query: LyricsQuery) -> Lyrics:
        close = [c for c in candidates if self._duration_ok(c, query)]

        def distance(c: dict[str, Any]) -> float:
            try:
                return abs(float(c["duration"]) - (query.duration or 0.0)) if query.duration else 0.0
            except (KeyError, TypeError, ValueError):
                return 0.0

        for item in sorted((c for c in close if c.get("syncedLyrics")), key=distance):
            lines = parse_lrc(str(item["syncedLyrics"]))
            if any(ln.text for ln in lines):
                return Lyrics(LyricsStatus.SYNCED, tuple(lines), source=self.name)
        if any(c.get("instrumental") for c in close):
            return Lyrics(LyricsStatus.INSTRUMENTAL, source=self.name)
        if any(c.get("plainLyrics") for c in close):
            return Lyrics(LyricsStatus.UNSYNCED, source=self.name,
                          message="Lyrics exist but are not time-synchronized, so they are not shown.")
        return Lyrics(LyricsStatus.NOT_FOUND, source=self.name)


class LyricsCache:
    """On-disk cache of successful lookups (JSON, one file per track)."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, query: LyricsQuery) -> Path:
        return self._dir / f"{query.cache_key}.json"

    def get(self, query: LyricsQuery) -> Lyrics | None:
        try:
            data = json.loads(self._path(query).read_text(encoding="utf-8"))
            return Lyrics(
                LyricsStatus(data["status"]),
                tuple(LyricLine(float(t), str(x)) for t, x in data.get("lines", [])),
                source=str(data.get("source", "")),
                message=str(data.get("message", "")),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, query: LyricsQuery, lyrics: Lyrics) -> None:
        if lyrics.status not in (LyricsStatus.SYNCED, LyricsStatus.INSTRUMENTAL,
                                 LyricsStatus.UNSYNCED):
            return
        payload = {"status": lyrics.status.value, "source": lyrics.source,
                   "message": lyrics.message, "lines": [[ln.time, ln.text] for ln in lyrics.lines]}
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path(query).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            _log.warning("Could not write lyrics cache: %s", exc)


class LyricsService:
    """Cache + ordered provider chain. The first provider returning synced lyrics wins.

    Results are memoised per instance and cached on disk, so a track is requested at most once.
    """

    def __init__(self, providers: list[LyricsProvider], cache: LyricsCache | None = None) -> None:
        self._providers = providers
        self._cache = cache
        self._memo: dict[str, Lyrics] = {}

    def fetch(self, meta: MediaMetadata) -> Lyrics:
        """Find lyrics for *meta*. Never raises."""
        query = LyricsQuery.from_metadata(meta)
        if is_generic_title(query.title):
            return Lyrics(LyricsStatus.NOT_FOUND, message=(
                "No track title to search with. Name the file 'Artist - Title.mp4' "
                "or use --title / --artist."))
        if query.cache_key in self._memo:
            return self._memo[query.cache_key]
        cached = self._cache.get(query) if self._cache else None
        if cached is not None:
            self._memo[query.cache_key] = cached
            return cached
        best: Lyrics | None = None
        for provider in self._providers:
            try:
                result = provider.fetch(query)
            except Exception as exc:  # providers should not raise, but never crash playback
                _log.warning("Lyrics provider %s failed: %s", provider.name, type(exc).__name__)
                continue
            if result.is_synced:
                best = result
                break
            if best is None or _rank(result) > _rank(best):
                best = result
        best = best or Lyrics(LyricsStatus.NOT_FOUND)
        if self._cache:
            self._cache.put(query, best)
        if best.status is not LyricsStatus.ERROR:
            self._memo[query.cache_key] = best
        return best


def _rank(lyrics: Lyrics) -> int:
    order = [LyricsStatus.ERROR, LyricsStatus.NOT_FOUND, LyricsStatus.UNSYNCED,
             LyricsStatus.INSTRUMENTAL, LyricsStatus.SYNCED]
    return order.index(lyrics.status)
