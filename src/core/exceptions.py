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


class MediaError(AsciiPlayerError):
    """Base class for media resolution problems."""


class InvalidURLError(MediaError):
    """Raised when a URL is malformed or not from a supported site."""


class UnsupportedMediaError(MediaError):
    """Raised for file types or sources the player cannot handle."""


class MediaUnavailableError(MediaError):
    """Raised when a source exists but no permitted playable media can be obtained."""


class AudioError(AsciiPlayerError):
    """Base class for audio playback problems."""


class MpvNotFoundError(AudioError):
    """Raised when the mpv executable cannot be located."""


class AudioPlaybackError(AudioError):
    """Raised when mpv fails to start, disconnects, or reports a playback error."""
