"""Checks for external programs."""

from __future__ import annotations

import shutil


def ffmpeg_available() -> bool:
    """Whether ffprobe/ffmpeg (FFmpeg) are on PATH. Used for tags and durations of local files."""
    return shutil.which("ffprobe") is not None or shutil.which("ffmpeg") is not None
