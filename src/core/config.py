"""Centralised, immutable application settings and quality profiles."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from src.core.exceptions import ConfigError

#: Dark -> bright, 10 levels. Fast and clean.
CHARSET_STANDARD = " .:-=+*#%@"
#: Dark -> bright, 70 levels (classic "high detail" ramp, for dark terminals).
CHARSET_HIGH = " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$"
#: Every third character of the 70-level ramp: 24 levels, still monotonic in density.
CHARSET_BALANCED = CHARSET_HIGH[::3]


@dataclass(frozen=True, slots=True)
class QualityProfile:
    """Everything a quality level changes.

    Attributes:
        charset: Characters ordered dark -> bright.
        max_width: Widest picture (in characters) this profile will draw.
        ui_fps: Maximum screen redraws per second. The player shows every k-th video frame
            (k a whole number) so that this cap is respected with an even cadence; video frames
            are still chosen by the audio clock, so a lower value only makes the picture less
            fluid, never late.
        interpolation: ``"area"`` (anti-aliased, slower) or ``"linear"`` (fast, can alias).
        description: One line for ``--help`` / the README.
        chunk: Picture block size. ``2`` samples the video at half resolution in both directions
            and draws every sample as a 2x2 block of characters (chunky, cheap); ``1`` uses every
            terminal cell as its own sample.
        enhance: Detail enhancement applied to brightness before it becomes characters:
            ``"none"``; ``"levels"`` (smoothly stretch the picture's brightness range so details use
            the whole character ramp); ``"detail"`` (levels plus local contrast and sharpening).
        compact: Picture-first layout: thin header and footer without borders, so the picture
            gets many more rows and columns.
    """

    charset: str
    max_width: int
    ui_fps: float
    interpolation: str
    description: str
    chunk: int = 1
    enhance: str = "none"
    compact: bool = False


class Quality(Enum):
    """Rendering quality profiles."""

    LOW = QualityProfile(CHARSET_STANDARD, 100, 15.0, "linear",
                         "chunky 2x2-character blocks, 10-level characters, fast, 15 redraws/s",
                         chunk=2, enhance="none", compact=False)
    BALANCED = QualityProfile(CHARSET_BALANCED, 160, 30.0, "area",
                              "every cell a sample, 24-level characters, auto-contrast, 30 redraws/s",
                              chunk=1, enhance="levels", compact=False)
    HIGH = QualityProfile(CHARSET_HIGH, 320, 30.0, "area",
                          "picture-first layout (most characters on screen), 70-level characters, "
                          "local contrast and sharpening, 30 redraws/s",
                          chunk=1, enhance="detail", compact=True)

    @property
    def profile(self) -> QualityProfile:
        return self.value  # type: ignore[no-any-return]

    @property
    def charset(self) -> str:
        return self.profile.charset

    @property
    def label(self) -> str:
        """Short name shown in the player: ``low`` / ``medium`` / ``high``."""
        return "medium" if self is Quality.BALANCED else self.name.lower()

    @classmethod
    def parse(cls, name: str) -> Quality:
        """Case-insensitive lookup. ``medium`` and the legacy ``standard`` mean ``balanced``.

        Raises:
            ConfigError: for an unknown name.
        """
        key = str(name).strip().upper()
        if key in ("STANDARD", "MEDIUM"):
            key = "BALANCED"
        try:
            return cls[key]
        except KeyError:
            choices = ", ".join(q.label for q in cls)
            raise ConfigError(f"Unknown quality '{name}'. Choose one of: {choices}.") from None


DEFAULT_CACHE_DIR = Path("data") / "cache"  # replaced by the platform default in utils.paths


@dataclass(frozen=True, slots=True)
class AppConfig:
    """All user-tunable settings. Use :meth:`with_overrides` to derive variants.

    Settings come from three layers, later ones winning: built-in defaults, the user's
    ``config.toml`` (see :mod:`src.core.user_config`) and command-line flags.
    """

    #: Maximum picture width in characters; ``None`` = the quality profile's width.
    ascii_width: int | None = None
    quality: Quality = Quality.BALANCED
    #: Overrides the quality profile's character set when set (dark -> bright).
    charset: str | None = None
    #: Target FPS override for silent mode. ``None`` uses the video's native FPS.
    fps: float | None = None
    max_fps: float = 60.0
    color: bool = False
    invert: bool = False
    #: Suppress character shimmer caused by video compression noise (see ASCIIConverter).
    stabilize: bool = True
    #: Width/height ratio correction: terminal cells are about twice as tall as wide.
    char_aspect: float = 0.5
    #: Render in the terminal's alternate screen (restores the terminal afterwards).
    alt_screen: bool = True
    #: Allow dropping frames to stay in sync when rendering is slow.
    drop_frames: bool = True
    show_stats: bool = True
    # --- audio / lyrics / controls ---
    volume: int = 80
    volume_step: int = 5
    seek_step: float = 5.0
    show_lyrics: bool = True
    show_video: bool = True
    lyrics_enabled: bool = True
    lyrics_timeout: float = 20.0
    lyrics_api_url: str = "https://lrclib.net/api"
    lyrics_cache_max_entries: int = 500
    #: Explicit path to the mpv executable; ``None`` searches PATH and common locations.
    mpv_path: str | None = None
    #: Maximum screen redraws per second; ``None`` = the quality profile's value.
    ui_fps: float | None = None
    # --- storage / diagnostics ---
    #: Where lyrics and logs are stored; ``None`` = the platform default.
    cache_dir: Path | None = None
    log_enabled: bool = True
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.ascii_width is not None and not 10 <= self.ascii_width <= 500:
            raise ConfigError("width must be between 10 and 500 (or 0 for automatic)")
        if not (math.isfinite(self.char_aspect) and 0.1 <= self.char_aspect <= 2.0):
            raise ConfigError("char_aspect must be between 0.1 and 2.0")
        if self.fps is not None and not 0 < self.fps <= 10000:
            raise ConfigError("fps must be between 0 and 10000")
        if not 0 <= self.volume <= 130:
            raise ConfigError("volume must be between 0 and 130")
        if not 1 <= self.volume_step <= 50:
            raise ConfigError("volume_step must be between 1 and 50")
        if not 0.5 <= self.seek_step <= 600:
            raise ConfigError("seek_step must be between 0.5 and 600 seconds")
        if self.ui_fps is not None and not 1 <= self.ui_fps <= 120:
            raise ConfigError("ui fps must be between 1 and 120 (or 0 for automatic)")
        if not 1 <= self.lyrics_timeout <= 120:
            raise ConfigError("lyrics timeout must be between 1 and 120 seconds")
        if self.lyrics_cache_max_entries < 0:
            raise ConfigError("lyrics_cache_max_entries cannot be negative")
        if self.charset is not None and len(self.charset) < 2:
            raise ConfigError("charset needs at least 2 characters")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError("log level must be DEBUG, INFO, WARNING or ERROR")
        if not self.lyrics_api_url.lower().startswith(("http://", "https://")):
            raise ConfigError("lyrics api_url must start with http:// or https://")

    @property
    def active_charset(self) -> str:
        """Character ramp in effect (custom charset or the quality profile's)."""
        return self.charset if self.charset else self.quality.charset

    @property
    def effective_width(self) -> int:
        """Maximum picture width: the explicit setting, else the quality profile's."""
        return self.ascii_width or self.quality.profile.max_width

    @property
    def effective_ui_fps(self) -> float:
        """Maximum redraws per second: the explicit setting, else the quality profile's."""
        return self.ui_fps or self.quality.profile.ui_fps

    @property
    def interpolation(self) -> str:
        return self.quality.profile.interpolation

    @property
    def chunk(self) -> int:
        return self.quality.profile.chunk

    @property
    def enhance(self) -> str:
        return self.quality.profile.enhance

    @property
    def compact(self) -> bool:
        return self.quality.profile.compact

    def with_overrides(self, **changes: object) -> AppConfig:
        """Return a validated copy with the given fields replaced."""
        return replace(self, **changes)  # type: ignore[arg-type]


DEFAULT_CONFIG = AppConfig()
