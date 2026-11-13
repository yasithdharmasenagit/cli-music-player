"""Logging setup (no global mutable state beyond the stdlib logging registry)."""

from __future__ import annotations

import logging
from pathlib import Path

from src.utils.paths import CACHE_DIR

_ROOT_NAME = "ascii_player"


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a namespaced logger."""
    return logging.getLogger(f"{_ROOT_NAME}.{name}" if name else _ROOT_NAME)


def configure_logging(to_file: bool = False, level: int = logging.INFO,
                      log_dir: Path = CACHE_DIR) -> None:
    """Attach a file handler (idempotent). Never logs to the console, which would
    corrupt the rendered video."""
    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.NullHandler())
    if to_file and not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "player.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
