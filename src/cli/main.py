"""Command-line entry point and workflow orchestration."""

from __future__ import annotations

import argparse
import platform
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from rich.markup import escape

from src._version import __version__
from src.audio.audio_controller import AudioController
from src.audio.audio_player import MpvBackend, find_mpv
from src.audio.playback_clock import PlaybackClock
from src.cli.interface import Interface
from src.core.config import DEFAULT_CONFIG, AppConfig, Quality
from src.core.exceptions import (
    AsciiPlayerError,
    InvalidURLError,
    MediaUnavailableError,
    MpvNotFoundError,
)
from src.core.user_config import (
    describe_config,
    load_config_file,
    write_example_config,
)
from src.lyrics.lyrics_client import LrclibProvider, LyricsCache, LyricsService
from src.lyrics.lyrics_models import Lyrics
from src.media.media_resolver import MediaResolver, ResolvedMedia
from src.media.youtube_resolver import looks_like_url
from src.player.session import PlaybackSession, SessionResult
from src.ui.keyboard import KeyReader
from src.ui.terminal_renderer import TerminalRenderer
from src.utils.dependencies import checks_exit_code, ffmpeg_available, run_checks
from src.utils.logger import configure_logging, get_logger
from src.utils.paths import default_cache_dir, default_config_path, validate_media_path
from src.video.ascii_converter import ASCIIConverter
from src.video.video_loader import VideoLoader
from src.video.video_player import PlaybackStats, VideoPlayer

_log = get_logger("cli")

QUALITY_CHOICES = ["low", "medium", "balanced", "high", "standard"]  # medium/standard = balanced


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser. Options default to ``None`` so the config file can supply them."""
    p = argparse.ArgumentParser(
        prog="ascii-music-player",
        description="Play music videos as ASCII art, with audio and synchronized lyrics.",
        epilog="Settings can be stored in a config file; see --init-config. "
               "Command-line options override the file.")
    p.add_argument("path", nargs="?", help="YouTube URL or local media path (prompted if omitted)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    tools = p.add_argument_group("setup and diagnostics")
    tools.add_argument("--doctor", action="store_true", help="check dependencies and environment, then exit")
    tools.add_argument("--init-config", action="store_true",
                       help="create a commented config file (if missing), then exit")
    tools.add_argument("--show-config", action="store_true", help="show the effective settings, then exit")
    tools.add_argument("--config", metavar="FILE", help="use this config file instead of the default")

    video = p.add_argument_group("picture")
    video.add_argument("-q", "--quality", choices=QUALITY_CHOICES,
                       help="low = fast, medium (= balanced, default), high = most detail")
    video.add_argument("-w", "--width", type=int, help="maximum ASCII width in characters (0 = automatic)")
    video.add_argument("--charset", help="custom characters, dark to bright (overrides the quality's)")
    video.add_argument("--color", action=argparse.BooleanOptionalAction, default=None,
                       help="coloured ANSI output")
    video.add_argument("--invert", action=argparse.BooleanOptionalAction, default=None,
                       help="invert brightness")
    video.add_argument("--stabilize", action=argparse.BooleanOptionalAction, default=None,
                       help="calm shimmer from video compression noise (default on)")
    video.add_argument("--video", dest="show_video", action=argparse.BooleanOptionalAction,
                       default=None, help="show the ASCII video at start (--no-video hides it)")
    video.add_argument("--ui-fps", type=float, help="maximum screen redraws per second (0 = automatic)")
    video.add_argument("--alt-screen", action=argparse.BooleanOptionalAction, default=None,
                       help="use the terminal's alternate screen")

    audio = p.add_argument_group("audio and lyrics")
    audio.add_argument("--volume", type=int, help="initial volume 0-130")
    audio.add_argument("--mpv-path", help="path to the mpv executable")
    audio.add_argument("--lyrics", dest="lyrics_enabled", action=argparse.BooleanOptionalAction,
                       default=None, help="look up lyrics (--no-lyrics disables)")
    audio.add_argument("--title", help="track title to use for the lyrics search")
    audio.add_argument("--artist", help="artist to use for the lyrics search")

    misc = p.add_argument_group("storage and logging")
    misc.add_argument("--cache-dir", help="folder for cached lyrics and logs")
    misc.add_argument("--no-log", action="store_true", help="do not write a log file")
    misc.add_argument("--log-level", choices=["debug", "info", "warning", "error"],
                      help="log verbosity (default info)")

    silent = p.add_argument_group("silent mode")
    silent.add_argument("--no-audio", action="store_true",
                        help="play a local video silently (no mpv needed)")
    silent.add_argument("--fps", type=float, help="override playback FPS (silent mode)")
    silent.add_argument("--no-stats", action="store_true", help="hide the status line (silent mode)")
    silent.add_argument("--no-drop", action="store_true", help="never drop frames (silent mode)")
    return p


def config_from_args(args: argparse.Namespace, base: AppConfig = DEFAULT_CONFIG) -> AppConfig:
    """Layer explicitly given command-line options on top of *base* (defaults + config file)."""
    changes: dict[str, object] = {}
    if args.quality is not None:
        changes["quality"] = Quality.parse(args.quality)
    if args.width is not None:
        changes["ascii_width"] = args.width or None
    if args.ui_fps is not None:
        changes["ui_fps"] = args.ui_fps or None
    if args.charset is not None:
        changes["charset"] = args.charset or None
    if args.cache_dir is not None:
        changes["cache_dir"] = Path(args.cache_dir).expanduser()
    if args.log_level is not None:
        changes["log_level"] = args.log_level.upper()
    if args.no_log:
        changes["log_enabled"] = False
    if args.no_stats:
        changes["show_stats"] = False
    if args.no_drop:
        changes["drop_frames"] = False
    for name in ("color", "invert", "stabilize", "show_video", "alt_screen", "lyrics_enabled"):
        if getattr(args, name) is not None:
            changes[name] = getattr(args, name)
    for name in ("volume", "mpv_path", "fps"):
        if getattr(args, name) is not None:
            changes[name] = getattr(args, name)
    return base.with_overrides(**changes)


def load_settings(args: argparse.Namespace) -> tuple[AppConfig, list[str], Path, bool]:
    """Defaults < config file < command line.

    Returns:
        ``(config, warnings, config_path, config_file_exists)``

    Raises:
        ConfigError: invalid config file or option value.
    """
    explicit = Path(args.config).expanduser() if args.config else None
    path = explicit or default_config_path()
    file_config, warnings = load_config_file(path, DEFAULT_CONFIG, must_exist=explicit is not None)
    return config_from_args(args, file_config), warnings, path, path.exists()


# --------------------------------------------------------------------------- silent (Phase 1)


def play_file(raw_path: str, config: AppConfig, ui: Interface) -> PlaybackStats:
    """Silent mode: validate, load and play one local video without audio.

    Raises:
        AsciiPlayerError: on any expected failure (bad path, bad video, bad frame).
    """
    path = validate_media_path(raw_path)
    ui.show_loading(path.name)
    renderer = TerminalRenderer(config, ui.console)
    with VideoLoader(path) as loader:
        info = loader.info
        width = min(config.effective_width, renderer.available_cols())
        converter = ASCIIConverter(
            width=width, charset=config.active_charset, invert=config.invert,
            color=config.color, char_aspect=config.char_aspect,
            max_rows=renderer.available_rows(), interpolation=config.interpolation,
            stabilize=config.stabilize, chunk=config.chunk, enhance=config.enhance,
        )
        cols, rows = converter.output_size(info.width, info.height)
        ui.show_video_info(info, cols, rows)
        time.sleep(0.4)  # let the user read the info line before the screen switches
        return VideoPlayer(loader, converter, renderer, config).play()


# --------------------------------------------------------------------------- full player


def cache_dir_for(config: AppConfig) -> Path:
    """The folder used for lyrics and logs."""
    return config.cache_dir or default_cache_dir()


def make_lyrics_fetcher(media: ResolvedMedia, config: AppConfig) -> Callable[[], Lyrics]:
    """Return a zero-arg callable that looks up lyrics (cached) for *media*."""
    provider = LrclibProvider(config.lyrics_api_url, config.lyrics_timeout)
    service = LyricsService(
        [provider],
        LyricsCache(cache_dir_for(config) / "lyrics", config.lyrics_cache_max_entries),
    )

    def fetch() -> Lyrics:
        try:
            return service.fetch(media.metadata)
        finally:
            provider.close()  # release the HTTP connection pool

    return fetch


def play_media(raw: str, config: AppConfig, ui: Interface, title: str | None = None,
               artist: str | None = None) -> SessionResult:
    """Resolve *raw* (URL or path) and play it with audio, ASCII video and lyrics.

    Every call builds fresh components, so repeated play requests never share state.

    Raises:
        AsciiPlayerError: any expected failure, with a user-presentable message.
    """
    mpv = find_mpv(config.mpv_path)  # fail fast, before any network work
    if not ffmpeg_available():
        ui.show_notice("FFmpeg not found: tags/duration of local files will be limited "
                       "(playback is unaffected).")
    ui.step("Reading media information")
    media = MediaResolver().resolve(raw)
    if title or artist:
        media = replace(media, metadata=replace(
            media.metadata, title=title or media.metadata.title,
            artist=artist or media.metadata.artist))
    ui.step("Preparing playback")
    ui.show_notice(f"{media.metadata.display}"
                   + (f"  ({int(media.metadata.duration // 60)}:{int(media.metadata.duration % 60):02d})"
                      if media.metadata.duration else "")
                   + ("" if media.has_video else "  · audio only"))
    clock = PlaybackClock()
    backend = MpvBackend(clock, mpv_path=mpv)
    audio = AudioController(backend, clock, volume=config.volume, volume_step=config.volume_step,
                            seek_step=config.seek_step)
    fetch = make_lyrics_fetcher(media, config) if config.lyrics_enabled else None
    if fetch is not None:
        ui.step("Searching for lyrics")
    ui.step("Starting player")
    session = PlaybackSession(media, config, audio, fetch, ui.console, KeyReader())
    return session.run()


def _report_session(result: SessionResult, ui: Interface) -> int:
    if result.notice:
        ui.show_warning(result.notice)
    if result.error:
        ui.show_error(escape(result.error))
        return 1
    if result.stalls:
        ui.show_warning(
            f"The screen stalled {result.stalls}x (longest {result.max_stall:.1f}s; slowest redraw "
            f"{result.max_render_ms:.0f} ms; longest gap between audio clock reports "
            f"{result.max_clock_gap:.2f}s). Lower the quality (-q low) or --ui-fps; details are in "
            f"the log file.")
    if result.video_recoveries:
        ui.show_notice(f"The video stream dropped {result.video_recoveries}x and was reconnected.")
    state = "stopped" if result.quit_by_user else "finished"
    ui.show_notice(f"Playback {state}. {result.frames_delivered} video frames shown, "
                   f"{result.frames_skipped} skipped to stay in sync.")
    return 0


def _explain(exc: AsciiPlayerError, raw: str, ui: Interface) -> None:
    ui.show_error(escape(str(exc)))
    if isinstance(exc, MediaUnavailableError) and looks_like_url(raw):
        ui.show_notice("Tip: you can play a local audio/video file instead; enter its path.")
    elif isinstance(exc, InvalidURLError):
        ui.show_notice("Paste a link such as https://www.youtube.com/watch?v=... or a file path.")
    elif isinstance(exc, MpvNotFoundError):
        ui.show_notice("Tip: run  ascii-music-player --doctor  to check your setup, or use "
                       "--no-audio to play a local video silently.")


def _ensure_utf8_streams() -> None:
    """Avoid UnicodeEncodeError on legacy code pages / redirected output."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _run_tool_command(args: argparse.Namespace, config: AppConfig, config_path: Path,
                      exists: bool, ui: Interface) -> int | None:
    """Handle --init-config / --show-config / --doctor. Returns an exit code, or None to play."""
    cache_dir = cache_dir_for(config)
    if args.init_config:
        created = write_example_config(config_path)
        ui.show_notice(f"{'Created' if created else 'Already exists (not overwritten)'}: {config_path}")
        return 0
    if args.show_config:
        for label, value in describe_config(config, config_path, exists, cache_dir):
            ui.console.print(f"[dim]{escape(label):<14}[/] {escape(value)}")
        return 0
    if args.doctor:
        checks = run_checks(config, config_path, cache_dir)
        ui.show_checks(checks)
        return checks_exit_code(checks)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Run the application. Returns a process exit code."""
    _ensure_utf8_streams()
    args = build_parser().parse_args(argv)
    ui = Interface()
    try:
        config, warnings, config_path, exists = load_settings(args)
    except AsciiPlayerError as exc:
        ui.show_error(escape(str(exc)))
        return 2
    log_path = configure_logging(config.log_enabled, config.log_level, cache_dir_for(config) / "logs")
    _log.info("ascii-music-player %s on Python %s, %s", __version__, platform.python_version(),
              platform.platform())
    for warning in warnings:
        _log.warning("config: %s", warning)
        ui.show_warning(warning)

    tool_result = _run_tool_command(args, config, config_path, exists, ui)
    if tool_result is not None:
        return tool_result
    if log_path is not None:
        _log.info("log file: %s", log_path)
    if not args.no_audio:
        run_checks(config, config_path, cache_dir_for(config))  # results go to the log file

    ui.show_banner()
    raw = args.path
    interactive = raw is None
    while True:
        if raw is None:
            raw = ui.prompt_media_path()
            if raw is None:
                return 0
        try:
            if args.no_audio:
                stats = play_file(raw, config, ui)
                ui.show_summary(stats)
                code = 0
            else:
                code = _report_session(play_media(raw, config, ui, args.title, args.artist), ui)
        except AsciiPlayerError as exc:
            _log.warning("Playback failed: %s: %s", type(exc).__name__, exc)
            _explain(exc, raw, ui)
            code = 1
        except KeyboardInterrupt:
            return 130
        except Exception as exc:  # unexpected: never leave a traceback on a restored terminal
            _log.exception("Unexpected failure")
            ui.show_error(escape(f"Unexpected error: {type(exc).__name__}: {exc}. "
                                 f"Details are in the log file."))
            code = 1
        if not interactive:
            return code
        raw = None  # interactive: ask for the next track (q quits)
        ui.console.print()


if __name__ == "__main__":
    sys.exit(main())
