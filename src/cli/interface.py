"""Interactive prompts and messages (Rich)."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from src.ui.colors import PALETTE
from src.ui.layout import build_summary, build_title
from src.utils.dependencies import Check
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
            raw = self.console.input(f"[{PALETTE.text}]Paste YouTube URL or media path:[/]\n[{PALETTE.accent}]>[/] ")
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

    def step(self, text: str) -> None:
        """Print a progress step like ``[+] Reading media information``."""
        self.console.print(f"[{PALETTE.accent}]\\[+][/] [{PALETTE.text}]{text}[/]")

    def show_notice(self, message: str) -> None:
        """Print a dim informational line."""
        self.console.print(f"[{PALETTE.dim}]{message}[/]")

    def show_warning(self, message: str) -> None:
        """Print a warning line."""
        self.console.print(f"[{PALETTE.warning}]![/] [{PALETTE.text}]{message}[/]")

    def show_checks(self, checks: list[Check]) -> None:
        """Print the ``--doctor`` report."""
        self.console.print(f"[{PALETTE.accent}]ASCII MUSIC PLAYER[/] [{PALETTE.dim}]environment check[/]")
        self.console.print()
        for c in checks:
            mark = f"[{PALETTE.accent}]✓[/]" if c.ok else (
                f"[{PALETTE.error}]✗[/]" if c.required else f"[{PALETTE.warning}]![/]")
            self.console.print(f"{mark} [{PALETTE.text}]{escape(c.name):<18}[/] [{PALETTE.dim}]{escape(c.detail)}[/]")
            if not c.ok and c.hint:
                self.console.print(f"    [{PALETTE.warning}]{escape(c.hint)}[/]")
        failed = [c for c in checks if c.required and not c.ok]
        self.console.print()
        if failed:
            self.console.print(f"[{PALETTE.error}]{len(failed)} required check(s) failed.[/]")
        else:
            self.console.print(f"[{PALETTE.accent}]Everything required is in place.[/]")
