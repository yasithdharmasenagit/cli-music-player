"""Filesystem helpers."""

from __future__ import annotations

from pathlib import Path

from src.core.exceptions import InvalidMediaPathError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
SAMPLES_DIR = PROJECT_ROOT / "assets" / "samples"


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
