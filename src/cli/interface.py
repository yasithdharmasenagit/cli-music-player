"""Interactive prompts and messages (Rich)."""

from __future__ import annotations

from rich.console import Console

from src.ui.colors import PALETTE
from src.ui.layout import build_summary, build_title
from src.video.video_loader import VideoInfo
from src.video.video_player import PlaybackStats

QUIT_WORDS = frozenset({"q", "quit", "exit"})


class Interface:
    """User-facing text I/O, separate from playback logic."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def show_banner(self) -> None:
        """Print the title."""
        self.console.print()
        self.console.print(build_title())
        self.console.print()

    def prompt_media_path(self) -> str | None:
        """Ask for a path. Returns ``None`` on quit/EOF/Ctrl+C."""
        try:
            raw = self.console.input(f"[{PALETTE.text}]Enter media path:[/]\n[{PALETTE.accent}]>[/] ")
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            return None
        if raw.strip().lower() in QUIT_WORDS:
            return None
        return raw

    def show_loading(self, path_name: str) -> None:
        """Announce loading."""
        self.console.print(f"[{PALETTE.accent}]Loading[/] [{PALETTE.text}]{path_name}[/] …")

    def show_video_info(self, info: VideoInfo, cols: int, rows: int) -> None:
        """Describe the loaded video and chosen ASCII grid."""
        duration = f"{info.duration:.1f}s" if info.duration else "unknown length"
        self.console.print(
            f"[{PALETTE.dim}]{info.width}x{info.height} · {info.fps:.2f} fps · "
            f"{info.frame_count or '?'} frames · {duration} → {cols}x{rows} characters[/]"
        )
        self.console.print(f"[{PALETTE.success}]Ready.[/] [{PALETTE.dim}]Starting playback…[/]")

    def show_error(self, message: str) -> None:
        """Print an error message."""
        self.console.print(f"[{PALETTE.error}]Error:[/] [{PALETTE.text}]{message}[/]", markup=True,
                           highlight=False)

    def show_summary(self, stats: PlaybackStats) -> None:
        """Print playback summary."""
        self.console.print(build_summary(stats))
