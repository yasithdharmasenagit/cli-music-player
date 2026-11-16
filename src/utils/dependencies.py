"""Dependency and environment checks (used by ``--doctor`` and logged at startup)."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from src.core.config import AppConfig
from src.core.exceptions import MpvNotFoundError
from src.utils.logger import get_logger

_log = get_logger("doctor")

MIN_PYTHON = (3, 12)


@dataclass(frozen=True, slots=True)
class Check:
    """Result of one check. ``required`` checks make ``--doctor`` fail when they do not pass."""

    name: str
    ok: bool
    detail: str
    required: bool = True
    hint: str = ""


def ffmpeg_available() -> bool:
    """Whether ffprobe/ffmpeg (FFmpeg) are on PATH. Used for tags and durations of local files."""
    return shutil.which("ffprobe") is not None or shutil.which("ffmpeg") is not None


def _package_check(module: str, label: str, required: bool = True, dist: str | None = None) -> Check:
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        return Check(label, False, f"not installed ({exc.name or module})", required,
                     hint='Run: pip install -e ".[dev]"  (or pip install -r requirements.txt)')
    version = getattr(mod, "__version__", None)
    if version is None and dist:
        try:
            version = metadata.version(dist)
        except metadata.PackageNotFoundError:
            version = None
    if version is None and module == "yt_dlp":
        version = getattr(importlib.import_module("yt_dlp.version"), "__version__", "")
    return Check(label, True, str(version or "installed"), required)


def _mpv_check(configured: str | None) -> Check:
    from src.audio.audio_player import find_mpv  # local import: keeps this module light

    try:
        exe = find_mpv(configured)
    except MpvNotFoundError:
        return Check("mpv", False, "not found", True,
                     "Install: winget install shinchiro.mpv  (or https://mpv.io/installation/), "
                     "then restart the terminal or set audio.mpv_path in the config.")
    version = ""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.run([exe, "--version"], capture_output=True, encoding="utf-8",
                              errors="replace", timeout=5, creationflags=flags, check=False)
        first_line = (proc.stdout.splitlines() or [""])[0]
        version = first_line.split(" Copyright")[0].strip()
    except (OSError, subprocess.SubprocessError):
        version = "(version unknown)"
    return Check("mpv", True, f"{version} - {exe}")


def _cache_check(cache_dir: Path) -> Check:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache_dir, prefix=".write-test-"):
            pass
    except OSError as exc:
        return Check("cache folder", False, f"{cache_dir} is not writable ({exc.strerror})", False,
                     "Lyrics caching and logging are disabled; set paths.cache_dir to a writable folder.")
    return Check("cache folder", True, str(cache_dir), False)


def run_checks(config: AppConfig, config_path: Path, cache_dir: Path) -> list[Check]:
    """Run every dependency/environment check. Never raises."""
    checks: list[Check] = []
    py_ok = sys.version_info[:2] >= MIN_PYTHON
    checks.append(Check("Python", py_ok, sys.version.split()[0] + f" ({sys.platform})",
                        True, "" if py_ok else f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required."))
    checks.append(_package_check("cv2", "OpenCV"))
    checks.append(_package_check("numpy", "NumPy"))
    checks.append(_package_check("rich", "Rich", dist="rich"))
    checks.append(_package_check("httpx", "httpx"))
    checks.append(_package_check("yt_dlp", "yt-dlp", required=False))
    checks.append(_mpv_check(config.mpv_path))
    has_ffmpeg = ffmpeg_available()
    checks.append(Check("FFmpeg (ffprobe)", has_ffmpeg,
                        shutil.which("ffprobe") or shutil.which("ffmpeg") or "not found", False,
                        "" if has_ffmpeg else "Optional. Without it local files show their file name "
                        "as the title and may lack a duration. Install: winget install Gyan.FFmpeg"))
    encoding = getattr(sys.stdout, "encoding", "") or ""
    checks.append(Check("Terminal", True,
                        ("Windows Terminal" if os.environ.get("WT_SESSION") else "terminal") +
                        f", {shutil.get_terminal_size().columns}x{shutil.get_terminal_size().lines}, "
                        f"{'interactive' if sys.stdout.isatty() else 'not interactive'}, "
                        f"encoding {encoding or 'unknown'}", False))
    exists = config_path.exists()
    checks.append(Check("Config file", True,
                        f"{config_path} ({'found' if exists else 'not created; defaults in use'})",
                        False))
    checks.append(_cache_check(cache_dir))
    for c in checks:
        _log.info("check %s: %s (%s)", c.name, "ok" if c.ok else "MISSING", c.detail)
    return checks


def checks_exit_code(checks: list[Check]) -> int:
    """0 when every required check passed, 1 otherwise."""
    return 0 if all(c.ok for c in checks if c.required) else 1
