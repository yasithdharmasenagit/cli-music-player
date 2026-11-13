"""Command-line entry point and workflow orchestration."""

from __future__ import annotations

import argparse
from dataclasses import replace
import sys
import time
from collections.abc import Sequence

from rich.markup import escape

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
from src.lyrics.lyrics_client import LrclibProvider, LyricsCache, LyricsService
from src.lyrics.lyrics_models import Lyrics
from src.media.media_resolver import MediaResolver, ResolvedMedia
from src.media.youtube_resolver import looks_like_url
from src.player.session import PlaybackSession, SessionResult
from src.ui.keyboard import KeyReader
from src.ui.terminal_renderer import TerminalRenderer
from src.utils.dependencies import ffmpeg_available
from src.utils.logger import configure_logging, get_logger
from src.utils.paths import CACHE_DIR, validate_media_path
from src.video.ascii_converter import ASCIIConverter
from src.video.video_loader import VideoLoader
from src.video.video_player import PlaybackStats, VideoPlayer

_log = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    p = argparse.ArgumentParser(prog="ascii-music-player",
                                description="Play music videos as ASCII art with audio and lyrics.")
    p.add_argument("path", nargs="?", help="YouTube URL or local media path (prompted if omitted)")
    p.add_argument("-w", "--width", type=int, default=DEFAULT_CONFIG.ascii_width,
                   help="maximum ASCII width in characters (default: %(default)s)")
    p.add_argument("-q", "--quality", choices=[q.name.lower() for q in Quality],
                   default=DEFAULT_CONFIG.quality.name.lower(), help="character detail level")
    p.add_argument("--charset", help="custom characters, dark to bright (overrides --quality)")
    p.add_argument("--fps", type=float, help="override playback FPS (silent mode only)")
    p.add_argument("--color", action="store_true", help="coloured ANSI output")
    p.add_argument("--invert", action="store_true", help="invert brightness")
    p.add_argument("--volume", type=int, default=DEFAULT_CONFIG.volume, help="initial volume 0-130")
    p.add_argument("--title", help="track title to use for the lyrics search")
    p.add_argument("--artist", help="artist to use for the lyrics search")
    p.add_argument("--ui-fps", type=float, default=DEFAULT_CONFIG.ui_fps,
                   help="maximum screen redraws per second (default: %(default)s); "
                        "lower it if the picture freezes")
    p.add_argument("--no-lyrics", action="store_true", help="do not search for lyrics")
    p.add_argument("--no-video", action="store_true", help="start with the ASCII video hidden")
    p.add_argument("--no-audio", action="store_true",
                   help="silent mode: local video only, no mpv needed (Phase 1 player)")
    p.add_argument("--mpv-path", help="path to the mpv executable")
    p.add_argument("--no-stats", action="store_true", help="hide the status line (silent mode)")
    p.add_argument("--no-alt-screen", action="store_true", help="render inline, not in the alternate screen")
    p.add_argument("--no-drop", action="store_true", help="never drop frames (silent mode)")
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
        volume=args.volume,
        ui_fps=args.ui_fps,
        lyrics_enabled=not args.no_lyrics,
        show_video=not args.no_video,
        mpv_path=args.mpv_path,
        show_stats=not args.no_stats,
        alt_screen=not args.no_alt_screen,
        drop_frames=not args.no_drop,
        log_to_file=args.log,
    )


# --------------------------------------------------------------------------- silent (Phase 1)


def play_file(raw_path: str, config: AppConfig, ui: Interface) -> PlaybackStats:
    """Phase 1 behaviour: validate, load and play one local video without audio.

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
        return VideoPlayer(loader, converter, renderer, config).play()


# --------------------------------------------------------------------------- full player


def make_lyrics_fetcher(media: ResolvedMedia, config: AppConfig):  # type: ignore[no-untyped-def]
    """Return a zero-arg callable that looks up lyrics (cached) for *media*."""
    service = LyricsService(
        [LrclibProvider(config.lyrics_api_url, config.lyrics_timeout)],
        LyricsCache(CACHE_DIR / "lyrics"),
    )

    def fetch() -> Lyrics:
        return service.fetch(media.metadata)

    return fetch


def play_media(raw: str, config: AppConfig, ui: Interface, title: str | None = None,
               artist: str | None = None) -> SessionResult:
    """Resolve *raw* (URL or path) and play it with audio, ASCII video and lyrics.

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
            f"{result.max_clock_gap:.2f}s). Re-run with --log and share data/cache/player.log.")
    if result.video_recoveries:
        ui.show_notice(f"The video stream dropped {result.video_recoveries}x and was reconnected.")
    state = "stopped" if result.quit_by_user else "finished"
    ui.show_notice(f"Playback {state}. {result.frames_decoded} video frames shown, "
                   f"{result.frames_skipped} skipped to stay in sync.")
    return 0


def _explain(exc: AsciiPlayerError, raw: str, ui: Interface) -> None:
    ui.show_error(escape(str(exc)))
    if isinstance(exc, MediaUnavailableError) and looks_like_url(raw):
        ui.show_notice("Tip: you can play a local audio/video file instead; enter its path.")
    elif isinstance(exc, InvalidURLError):
        ui.show_notice("Paste a link such as https://www.youtube.com/watch?v=... or a file path.")
    elif isinstance(exc, MpvNotFoundError):
        ui.show_notice("Tip: run with --no-audio to play a local video silently.")


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
            if args.no_audio:
                stats = play_file(raw, config, ui)
                ui.show_summary(stats)
                return 0
            return _report_session(play_media(raw, config, ui, args.title, args.artist), ui)
        except AsciiPlayerError as exc:
            _log.warning("Playback failed: %s", type(exc).__name__)
            _explain(exc, raw, ui)
            if not interactive:
                return 1
            raw = None
        except KeyboardInterrupt:
            return 130
        except Exception as exc:  # unexpected: never leave a traceback on a restored terminal
            _log.exception("Unexpected failure")
            ui.show_error(escape(f"Unexpected error: {type(exc).__name__}: {exc}"))
            return 1


if __name__ == "__main__":
    sys.exit(main())
