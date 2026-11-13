"""Command-line entry point and workflow orchestration."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence

from rich.markup import escape

from src.cli.interface import Interface
from src.core.config import DEFAULT_CONFIG, AppConfig, Quality
from src.core.exceptions import AsciiPlayerError
from src.ui.terminal_renderer import TerminalRenderer
from src.utils.logger import configure_logging, get_logger
from src.utils.paths import validate_media_path
from src.video.ascii_converter import ASCIIConverter
from src.video.video_loader import VideoLoader
from src.video.video_player import PlaybackStats, VideoPlayer

_log = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    p = argparse.ArgumentParser(prog="ascii-music-player",
                                description="Play videos as animated ASCII art in your terminal.")
    p.add_argument("path", nargs="?", help="local video file (prompted if omitted)")
    p.add_argument("-w", "--width", type=int, default=DEFAULT_CONFIG.ascii_width,
                   help="ASCII width in characters (default: %(default)s)")
    p.add_argument("-q", "--quality", choices=[q.name.lower() for q in Quality],
                   default=DEFAULT_CONFIG.quality.name.lower(), help="character detail level")
    p.add_argument("--charset", help="custom characters, dark to bright (overrides --quality)")
    p.add_argument("--fps", type=float, help="override playback FPS")
    p.add_argument("--color", action="store_true", help="coloured ANSI output")
    p.add_argument("--invert", action="store_true", help="invert brightness")
    p.add_argument("--no-stats", action="store_true", help="hide the status line")
    p.add_argument("--no-alt-screen", action="store_true", help="render inline, not in the alternate screen")
    p.add_argument("--no-drop", action="store_true", help="never drop frames (may drift out of sync)")
    p.add_argument("--log", action="store_true", help="write a log file to data/cache/")
    return p


def config_from_args(args: argparse.Namespace) -> AppConfig:
    """Translate parsed CLI arguments into an :class:`AppConfig`."""
    return DEFAULT_CONFIG.with_overrides(
        ascii_width=args.width,
        quality=Quality[args.quality.upper()],
        charset=args.charset,
        fps=args.fps,
        color=args.color,
        invert=args.invert,
        show_stats=not args.no_stats,
        alt_screen=not args.no_alt_screen,
        drop_frames=not args.no_drop,
        log_to_file=args.log,
    )


def play_file(raw_path: str, config: AppConfig, ui: Interface) -> PlaybackStats:
    """Validate, load and play one video.

    Raises:
        AsciiPlayerError: on any expected failure (bad path, bad video, bad frame).
    """
    path = validate_media_path(raw_path)
    ui.show_loading(path.name)
    renderer = TerminalRenderer(config, ui.console)
    with VideoLoader(path) as loader:
        info = loader.info
        width = min(config.ascii_width, renderer.available_cols())
        converter = ASCIIConverter(
            width=width, charset=config.active_charset, invert=config.invert,
            color=config.color, char_aspect=config.char_aspect,
            max_rows=renderer.available_rows(),
        )
        cols, rows = converter.output_size(info.width, info.height)
        ui.show_video_info(info, cols, rows)
        time.sleep(0.4)  # let the user read the info line before the screen switches
        player = VideoPlayer(loader, converter, renderer, config)
        return player.play()


def _ensure_utf8_streams() -> None:
    """Avoid UnicodeEncodeError on legacy code pages / redirected output."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: Sequence[str] | None = None) -> int:
    """Run the application. Returns a process exit code."""
    _ensure_utf8_streams()
    args = build_parser().parse_args(argv)
    ui = Interface()
    try:
        config = config_from_args(args)
    except AsciiPlayerError as exc:
        ui.show_error(escape(str(exc)))
        return 2
    configure_logging(config.log_to_file)

    ui.show_banner()
    raw = args.path
    interactive = raw is None
    while True:
        if raw is None:
            raw = ui.prompt_media_path()
            if raw is None:
                return 0
        try:
            stats = play_file(raw, config, ui)
        except AsciiPlayerError as exc:
            _log.warning("Playback failed: %s", exc)
            ui.show_error(escape(str(exc)))
            if not interactive:
                return 1
            raw = None
            continue
        except Exception as exc:  # unexpected: never leave a traceback on a restored terminal
            _log.exception("Unexpected failure")
            ui.show_error(escape(f"Unexpected error: {exc}"))
            return 1
        ui.show_summary(stats)
        return 0


if __name__ == "__main__":
    sys.exit(main())
