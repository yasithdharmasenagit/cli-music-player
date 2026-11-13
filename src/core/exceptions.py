"""Custom exception hierarchy for ASCII Music Player."""

from __future__ import annotations


class AsciiPlayerError(Exception):
    """Base class for all application errors."""


class InvalidFrameError(AsciiPlayerError):
    """Raised when a frame is None, empty, or has an unsupported shape/dtype."""


class InvalidMediaPathError(AsciiPlayerError):
    """Raised when a user-supplied media path is empty, missing, or not a file."""


class VideoLoadError(AsciiPlayerError):
    """Raised when a video file cannot be opened or contains no readable frames."""


class ConfigError(AsciiPlayerError):
    """Raised when configuration values are invalid."""
