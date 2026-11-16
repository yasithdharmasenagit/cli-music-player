"""User configuration file (TOML): loading, validation, example generation.

Precedence, lowest to highest: built-in defaults < ``config.toml`` < command-line flags.
Unknown keys produce warnings (typos should be visible, not fatal); wrong types or
out-of-range values raise :class:`~src.core.exceptions.ConfigError` naming the offending key.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.config import DEFAULT_CONFIG, AppConfig, Quality
from src.core.exceptions import ConfigError

EXAMPLE_CONFIG = """\
# ASCII Music Player configuration.
# Every setting is optional: delete a line to use the default.
# Command-line flags always override this file.
# Show the effective settings with:  python run.py --show-config

[video]
# Picture quality: "low" (fast), "balanced" (default) or "high" (most detail).
quality = "balanced"
# Maximum picture width in characters. 0 = automatic (set by the quality; always fitted to the window).
width = 0
# Coloured output (heavier on the terminal).
color = false
# Invert brightness (for light terminal themes).
invert = false
# Calm the picture: ignore tiny frame-to-frame brightness noise so still areas do not shimmer.
stabilize = true
# Custom character ramp from darkest to brightest. Empty = use the quality's ramp.
charset = ""

[audio]
# Initial volume, 0-130 (above 100 amplifies).
volume = 80
# How much the Up/Down keys change the volume.
volume_step = 5
# How many seconds the Left/Right keys seek.
seek_step = 5
# Full path to mpv.exe if it is not on your PATH. Empty = search automatically.
mpv_path = ""

[lyrics]
# Look up synchronized lyrics automatically.
enabled = true
# Show the lyrics panel when playback starts (toggle with the L key).
show = true
# Seconds to wait for the lyrics service before giving up (it runs in the background).
timeout = 20
api_url = "https://lrclib.net/api"
# How many tracks' lyrics to keep cached on disk (oldest are removed). 0 = do not cache at all.
cache_max_entries = 500

[ui]
# Show the ASCII video when playback starts (toggle with the V key).
show_video = true
# Maximum screen redraws per second. 0 = automatic (set by the quality).
fps = 0
# Use the terminal's alternate screen so your scrollback is untouched afterwards.
alt_screen = true

[paths]
# Folder for cached lyrics and logs. Empty = the default per-user location.
cache_dir = ""

[logging]
# Write a rotating log file (<cache_dir>/logs/player.log). Credentials and URL tokens are redacted.
enabled = true
# DEBUG, INFO, WARNING or ERROR.
level = "INFO"
"""


@dataclass(frozen=True, slots=True)
class _Setting:
    section: str
    key: str
    field: str
    kind: str  # bool | int | float | str | quality | width | fps | charset | opt_str | opt_path | level
    low: float | None = None
    high: float | None = None


_SCHEMA: tuple[_Setting, ...] = (
    _Setting("video", "quality", "quality", "quality"),
    _Setting("video", "width", "ascii_width", "width"),
    _Setting("video", "color", "color", "bool"),
    _Setting("video", "invert", "invert", "bool"),
    _Setting("video", "stabilize", "stabilize", "bool"),
    _Setting("video", "charset", "charset", "charset"),
    _Setting("audio", "volume", "volume", "int", 0, 130),
    _Setting("audio", "volume_step", "volume_step", "int", 1, 50),
    _Setting("audio", "seek_step", "seek_step", "float", 0.5, 600),
    _Setting("audio", "mpv_path", "mpv_path", "opt_str"),
    _Setting("lyrics", "enabled", "lyrics_enabled", "bool"),
    _Setting("lyrics", "show", "show_lyrics", "bool"),
    _Setting("lyrics", "timeout", "lyrics_timeout", "float", 1, 120),
    _Setting("lyrics", "api_url", "lyrics_api_url", "str"),
    _Setting("lyrics", "cache_max_entries", "lyrics_cache_max_entries", "int", 0, 100000),
    _Setting("ui", "show_video", "show_video", "bool"),
    _Setting("ui", "fps", "ui_fps", "fps"),
    _Setting("ui", "alt_screen", "alt_screen", "bool"),
    _Setting("paths", "cache_dir", "cache_dir", "opt_path"),
    _Setting("logging", "enabled", "log_enabled", "bool"),
    _Setting("logging", "level", "log_level", "level"),
)
_KNOWN: dict[str, set[str]] = {}
for _s in _SCHEMA:
    _KNOWN.setdefault(_s.section, set()).add(_s.key)


def _coerce(setting: _Setting, value: Any) -> Any:
    name = f"[{setting.section}] {setting.key}"
    kind = setting.kind

    def number(v: Any, integer: bool) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
            raise ConfigError(f"{name} must be {'a whole number' if integer else 'a number'}, "
                              f"got {v!r}.")
        return float(v)

    if kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{name} must be true or false, got {value!r}.")
        return value
    if kind in ("int", "float"):
        num = number(value, kind == "int")
        if setting.low is not None and not setting.low <= num <= (setting.high or num):
            raise ConfigError(f"{name} must be between {setting.low:g} and {setting.high:g}, "
                              f"got {value!r}.")
        return int(num) if kind == "int" else num
    if kind == "width":
        num = number(value, True)
        return None if num == 0 else int(num)
    if kind == "fps":
        num = number(value, False)
        return None if num == 0 else num
    if kind == "quality":
        if not isinstance(value, str):
            raise ConfigError(f"{name} must be a string such as \"balanced\", got {value!r}.")
        try:
            return Quality.parse(value)
        except ConfigError as exc:
            raise ConfigError(f"{name}: {exc}") from None
    if kind == "level":
        if not isinstance(value, str) or value.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError(f"{name} must be DEBUG, INFO, WARNING or ERROR, got {value!r}.")
        return value.upper()
    if kind == "charset":
        # Spaces are meaningful here (the darkest "character" is usually a space): keep as typed.
        if not isinstance(value, str):
            raise ConfigError(f"{name} must be text in quotes, got {value!r}.")
        return value if value.strip() else None
    if kind in ("str", "opt_str", "opt_path"):
        if not isinstance(value, str):
            raise ConfigError(f"{name} must be text in quotes, got {value!r}.")
        text = value.strip()
        if kind == "str":
            return text
        if not text:
            return None
        return Path(text).expanduser() if kind == "opt_path" else text
    raise AssertionError(kind)  # pragma: no cover


def parse_config_text(text: str, base: AppConfig = DEFAULT_CONFIG,
                      source: str = "config") -> tuple[AppConfig, list[str]]:
    """Apply TOML *text* on top of *base*.

    Returns:
        ``(config, warnings)``; warnings list unknown sections/keys.

    Raises:
        ConfigError: invalid TOML, wrong types, or out-of-range values.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source} is not valid TOML: {exc}") from None
    warnings: list[str] = []
    changes: dict[str, Any] = {}
    by_key = {(s.section, s.key): s for s in _SCHEMA}
    for section, values in data.items():
        if section not in _KNOWN:
            warnings.append(f"Unknown section [{section}] ignored.")
            continue
        if not isinstance(values, dict):
            raise ConfigError(f"[{section}] must be a table, e.g. [{section}] followed by key = value.")
        for key, value in values.items():
            setting = by_key.get((section, key))
            if setting is None:
                warnings.append(f"Unknown setting [{section}] {key} ignored.")
                continue
            changes[setting.field] = _coerce(setting, value)
    try:
        return base.with_overrides(**changes), warnings
    except ConfigError as exc:
        raise ConfigError(f"{source}: {exc}") from None


def load_config_file(path: Path, base: AppConfig = DEFAULT_CONFIG,
                     must_exist: bool = False) -> tuple[AppConfig, list[str]]:
    """Load *path*. A missing default file is fine; a missing explicit one (``must_exist``) is not.

    Raises:
        ConfigError: unreadable file, invalid contents.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if must_exist:
            raise ConfigError(f"Config file not found: {path}") from None
        return base, []
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc.strerror or exc}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"Config file {path} is not UTF-8 text.") from None
    return parse_config_text(text, base, source=str(path))


def write_example_config(path: Path, overwrite: bool = False) -> bool:
    """Write the commented example to *path*. Returns ``False`` if it exists and was kept.

    Raises:
        ConfigError: if the file cannot be written.
    """
    if path.exists() and not overwrite:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot write {path}: {exc.strerror or exc}") from None
    return True


def describe_config(config: AppConfig, path: Path, exists: bool,
                    cache_dir: Path) -> list[tuple[str, str]]:
    """Human-readable ``(label, value)`` rows for ``--show-config``."""
    profile = config.quality.profile
    return [
        ("config file", f"{path} ({'found' if exists else 'not created yet; defaults in use'})"),
        ("quality", f"{config.quality.name.lower()} - {profile.description}"),
        ("width", f"{config.effective_width} (max; always fitted to the window)"),
        ("color / invert", f"{config.color} / {config.invert}"),
        ("stabilize", f"{config.stabilize} (calms shimmer from compression noise)"),
        ("redraw rate", f"{config.effective_ui_fps:g} per second (max)"),
        ("volume", f"{config.volume} (step {config.volume_step}), seek step {config.seek_step:g}s"),
        ("lyrics", f"{'on' if config.lyrics_enabled else 'off'}, shown: {config.show_lyrics}, "
                   f"timeout {config.lyrics_timeout:g}s"),
        ("video shown", str(config.show_video)),
        ("mpv", config.mpv_path or "automatic"),
        ("cache folder", str(cache_dir)),
        ("logging", f"{'on' if config.log_enabled else 'off'} ({config.log_level.lower()})"),
    ]

