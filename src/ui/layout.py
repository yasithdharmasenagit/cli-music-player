"""Pure functions that build Rich renderables (no I/O)."""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text

from src.ui.colors import PALETTE
from src.video.video_player import PlaybackStats, PlaybackStatus


def format_time(seconds: float) -> str:
    """``mm:ss`` formatting."""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def build_title() -> Text:
    """The application banner."""
    title = Text()
    title.append("ASCII MUSIC PLAYER\n", style=PALETTE.accent)
    title.append("Watch music. Differently.", style=PALETTE.dim)
    return title


def build_status_line(status: PlaybackStatus) -> Text:
    """One-line playback status shown under the video."""
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append("▶ ", style=PALETTE.accent)
    if status.frame_count > 0:
        line.append(f"{status.frame_index}/{status.frame_count}  ", style=PALETTE.text)
    else:
        line.append(f"{status.frame_index}  ", style=PALETTE.text)
    line.append(format_time(status.elapsed), style=PALETTE.text)
    line.append("  │  ", style=PALETTE.dim)
    line.append(f"{status.actual_fps:4.1f}/{status.target_fps:.0f} fps", style=PALETTE.accent_alt)
    line.append("  │  ", style=PALETTE.dim)
    line.append(f"{status.process_ms:4.1f} ms/frame", style=PALETTE.text)
    line.append("  │  ", style=PALETTE.dim)
    line.append(f"dropped {status.dropped}", style=PALETTE.text)
    line.append("  │  Ctrl+C to stop", style=PALETTE.dim)
    return line


def build_frame_view(ascii_frame: str, status: PlaybackStatus | None,
                     color: bool = False) -> RenderableType:
    """ASCII frame plus optional status line."""
    if color:
        art = Text.from_ansi(ascii_frame, no_wrap=True, overflow="crop")
    else:
        art = Text(ascii_frame, style=PALETTE.ascii_art, no_wrap=True, overflow="crop")
    if status is None:
        return art
    return Group(art, build_status_line(status))


def build_summary(stats: PlaybackStats) -> Text:
    """Post-playback summary."""
    text = Text()
    state = "stopped by user" if stats.interrupted else "finished"
    text.append(f"Playback {state}.\n", style=PALETTE.accent)
    text.append(
        f"{stats.frames_shown} frames in {stats.elapsed:.1f}s  ·  "
        f"{stats.average_fps:.1f} fps (target {stats.target_fps:.1f})  ·  "
        f"avg {stats.avg_process_ms:.1f} ms/frame (max {stats.max_process_ms:.1f})  ·  "
        f"{stats.frames_dropped} dropped",
        style=PALETTE.text,
    )
    return text
