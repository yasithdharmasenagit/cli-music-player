"""Filesystem helpers and platform-specific default locations."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from src.core.exceptions import InvalidMediaPathError

APP_NAME = "ascii-music-player"
CONFIG_ENV_VAR = "ASCII_MUSIC_PLAYER_CONFIG"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = PROJECT_ROOT / "assets" / "samples"


def default_config_path(env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """Where the user's ``config.toml`` lives.

    ``ASCII_MUSIC_PLAYER_CONFIG`` overrides everything; otherwise
    ``%APPDATA%\\ascii-music-player\\config.toml`` on Windows and
    ``$XDG_CONFIG_HOME`` (default ``~/.config``) elsewhere.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get(CONFIG_ENV_VAR):
        return Path(env[CONFIG_ENV_VAR]).expanduser()
    if platform == "win32":
        base = Path(env.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME / "config.toml"


def default_cache_dir(env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """Default folder for cached lyrics and logs (never inside the install directory)."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / APP_NAME / "cache"
    base = Path(env.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / APP_NAME


def clean_user_path(raw: str) -> str:
    """Strip whitespace and the quotes/call-operator PowerShell adds to dragged paths."""
    text = raw.strip()
    if text.startswith("& "):  # PowerShell drag-drop form: & 'C:\a b.mp4'
        text = text[2:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text.strip()


def validate_media_path(raw: str | Path) -> Path:
    """Return an expanded, resolved path to an existing file or raise.

    Raises:
        InvalidMediaPathError: if the path is empty, missing, or not a file.
    """
    text = clean_user_path(str(raw))
    if not text:
        raise InvalidMediaPathError("No path was entered.")
    path = Path(text).expanduser()
    if not path.exists():
        raise InvalidMediaPathError(f"File not found: {path}")
    if not path.is_file():
        raise InvalidMediaPathError(f"Not a file: {path}")
    return path.resolve()
