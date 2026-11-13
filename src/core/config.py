"""Centralised, immutable application settings."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from src.core.exceptions import ConfigError

#: Dark -> bright, ~10 levels. Fast and clean.
CHARSET_STANDARD = " .:-=+*#%@"
#: Dark -> bright, 70 levels (classic "high detail" ramp, reversed for dark terminals).
CHARSET_HIGH = " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$"


class Quality(Enum):
    """Rendering quality presets; each maps to a default character set."""

    STANDARD = CHARSET_STANDARD
    HIGH = CHARSET_HIGH


@dataclass(frozen=True, slots=True)
class AppConfig:
    """All user-tunable settings. Use :meth:`with_overrides` to derive variants."""

    ascii_width: int = 120
    quality: Quality = Quality.HIGH
    #: Overrides the quality preset's character set when set (dark -> bright).
    charset: str | None = None
    #: Target FPS override. ``None`` uses the video's native FPS.
    fps: float | None = None
    max_fps: float = 60.0
    color: bool = False
    invert: bool = False
    #: Height/width ratio of a terminal cell (cells are ~2x taller than wide).
    char_aspect: float = 0.5
    #: Render in the terminal's alternate screen (restores terminal afterwards).
    alt_screen: bool = True
    #: Allow dropping frames to stay in sync when rendering is slow.
    drop_frames: bool = True
    show_stats: bool = True
    log_to_file: bool = False

    def __post_init__(self) -> None:
        if self.ascii_width < 10:
            raise ConfigError("ascii_width must be at least 10")
        if self.char_aspect <= 0:
            raise ConfigError("char_aspect must be positive")
        if self.fps is not None and self.fps <= 0:
            raise ConfigError("fps must be positive")
        if self.charset is not None and len(self.charset) < 2:
            raise ConfigError("charset needs at least 2 characters")

    @property
    def active_charset(self) -> str:
        """Character ramp in effect (custom charset or the quality preset)."""
        return self.charset if self.charset else self.quality.value

    def with_overrides(self, **changes: object) -> AppConfig:
        """Return a copy with the given fields replaced."""
        return replace(self, **changes)  # type: ignore[arg-type]


DEFAULT_CONFIG = AppConfig()
