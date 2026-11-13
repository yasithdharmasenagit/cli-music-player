"""Playback session: wires audio clock, ASCII video, lyrics, keyboard and the Rich screen."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from rich.console import Console
from rich.live import Live

from src.audio.audio_controller import AudioController
from src.core.config import AppConfig
from src.core.exceptions import VideoLoadError
from src.lyrics.lyrics_models import Lyrics
from src.lyrics.lyrics_sync import LyricsTrack
from src.media.media_resolver import ResolvedMedia
from src.ui.keyboard import Key, KeyReader
from src.ui.player_view import ViewState, build_player_view, video_area
from src.utils.logger import get_logger
from src.utils.stderr import suppress_native_stderr
from src.video.ascii_converter import ASCIIConverter
from src.video.video_loader import VideoLoader
from src.video.video_sync import BackgroundVideoSource, SyncedVideoSource

_log = get_logger("session")

LOAD_TIMEOUT = 15.0   # seconds to wait for mpv to report the first position
STALL_THRESHOLD = 0.5  # seconds without a UI loop iteration counts as a stall
ADAPTIVE_FACTOR = 3.0  # redraw at most every 3x the measured redraw cost (<= ~33% of the time)
IDLE_TICK = 0.005     # loop yield only; frame/lyric selection is driven by the audio clock


@dataclass(slots=True)
class SessionResult:
    """How a session ended."""

    quit_by_user: bool = False
    completed: bool = False
    error: str | None = None
    notice: str | None = None
    frames_decoded: int = 0
    frames_skipped: int = 0
    seeks: int = 0
    duration_played: float = 0.0
    #: Diagnostics: loop iterations that took longer than STALL_THRESHOLD, and worst timings.
    stalls: int = 0
    max_stall: float = 0.0
    max_render_ms: float = 0.0
    max_clock_gap: float = 0.0
    video_recoveries: int = 0


class PlaybackSession:
    """Runs one track until it ends, errors, or the user quits.

    Position always comes from ``audio.clock`` (mpv). The loop merely polls it: a video frame
    is decoded when the clock says a new one is due, lyrics are looked up by clock time, and
    the screen is redrawn only when something visible changed.
    """

    def __init__(self, media: ResolvedMedia, config: AppConfig, audio: AudioController,
                 lyrics_fetch: Callable[[], Lyrics] | None, console: Console,
                 keys: KeyReader, loader_factory: Callable[[str], VideoLoader] = VideoLoader) -> None:
        self._media = media
        self._config = config
        self._audio = audio
        self._console = console
        self._keys = keys
        self._loader_factory = loader_factory
        self._lyrics = LyricsTrack(lyrics_fetch if config.lyrics_enabled else None)
        self._state = ViewState(
            title=media.metadata.title, artist=media.metadata.artist, origin=media.origin,
            duration=media.metadata.duration, volume=audio.volume, color=config.color,
            show_video=config.show_video, show_lyrics=config.show_lyrics,
            audio_only=not media.has_video, loading="Preparing playback…",
        )
        self._loader: VideoLoader | None = None
        self._source: SyncedVideoSource | None = None
        self._bg: BackgroundVideoSource | None = None
        self._converter: ASCIIConverter | None = None
        self._raw_frame: np.ndarray | None = None
        self._size: tuple[int, int] = (0, 0)
        self._ascii_dirty = False
        self._result = SessionResult()
        self._live: Live | None = None
        self._render_ema = 0.0  # seconds

    # -- setup

    def _open_video(self) -> None:
        if not self._media.video_source:
            return
        try:
            loader = self._loader_factory(self._media.video_source)
            loader.open()
        except VideoLoadError as exc:
            _log.warning("Video unavailable: %s", exc)
            self._state.audio_only = True
            self._state.video_notice = "Video could not be decoded — playing audio only"
            self._result.notice = "Video could not be decoded; playing audio only."
            return
        self._loader = loader
        self._source = SyncedVideoSource(loader)
        self._bg = BackgroundVideoSource(self._source, self._audio.clock.position,
                                         pause=lambda: not self._state.show_video)

    def _rebuild_converter(self, size: tuple[int, int]) -> None:
        cols, rows = video_area(size[0], size[1], self._state.show_lyrics)
        self._converter = ASCIIConverter(
            width=min(self._config.ascii_width, cols), charset=self._config.active_charset,
            invert=self._config.invert, color=self._config.color,
            char_aspect=self._config.char_aspect, max_rows=rows)
        self._ascii_dirty = self._raw_frame is not None

    # -- main entry

    def run(self) -> SessionResult:
        """Play the media. Always releases mpv, the video file and the terminal.

        Raises:
            AudioError: if mpv cannot be started (raised before the screen is taken over).
        """
        self._open_video()
        self._audio.load(self._media.audio_source, self._media.user_agent)  # may raise AudioError
        self._lyrics.start()
        try:
            with self._keys, suppress_native_stderr(), Live(
                    console=self._console, screen=self._config.alt_screen,
                    auto_refresh=False, vertical_overflow="crop") as live:
                self._live = live
                if self._wait_until_ready():
                    if self._bg is not None:
                        self._bg.start()
                    self._audio.resume()
                    self._state.loading = None
                    self._loop()
        except KeyboardInterrupt:
            self._result.quit_by_user = True
        finally:
            self._audio.stop()
            if self._bg is not None:
                self._bg.stop()  # the decoder thread must be gone before the loader is released
            if self._loader is not None:
                self._loader.close()
        self._result.duration_played = self._audio.position
        self._result.max_clock_gap = self._audio.clock.max_update_gap
        if self._source is not None:
            self._result.frames_decoded = self._source.frames_decoded
            self._result.frames_skipped = self._source.frames_skipped
            self._result.seeks = self._source.seeks
            self._result.video_recoveries = self._source.recoveries
        if self._audio.error and not self._result.quit_by_user:
            self._result.error = self._audio.error
        return self._result

    def _render(self) -> None:
        size = (self._console.size.width, self._console.size.height)
        if size != self._size or self._converter is None:
            self._size = size
            self._rebuild_converter(size)
        if self._ascii_dirty and self._raw_frame is not None and self._converter is not None:
            try:
                self._state.ascii_frame = self._converter.convert(self._raw_frame)
            except Exception as exc:  # bad frame: keep the previous picture
                _log.warning("Frame conversion failed: %s", type(exc).__name__)
            self._ascii_dirty = False
        if self._live is not None:
            t0 = time.perf_counter()
            self._live.update(build_player_view(self._state, size[0]), refresh=True)
            took = (time.perf_counter() - t0) * 1000
            self._result.max_render_ms = max(self._result.max_render_ms, took)
            # Smoothed redraw cost, used to throttle redraws if the terminal is slow.
            self._render_ema = took / 1000 if self._render_ema == 0 else                 0.8 * self._render_ema + 0.2 * took / 1000

    def _wait_until_ready(self) -> bool:
        """Show the loading screen until mpv reports its first position."""
        deadline = time.monotonic() + LOAD_TIMEOUT
        self._render()
        while not self._audio.clock.started:
            if self._audio.error:
                return False
            for key in self._keys.poll():
                if key is Key.QUIT:
                    self._result.quit_by_user = True
                    return False
            if time.monotonic() > deadline:
                self._audio.clock.set_error("Timed out waiting for audio to start.")
                return False
            time.sleep(IDLE_TICK * 4)
        if self._source is not None:
            first = self._source.frame_at(0.0)
            if first is not None:
                self._raw_frame = first[1]
                self._ascii_dirty = True
        return True

    def _loop(self) -> None:
        clock = self._audio.clock
        min_interval = 1.0 / self._config.ui_fps
        last_render = 0.0
        last_second_tick = -1
        last_lyric = -2
        dirty = True
        previous_iteration = time.monotonic()
        while True:
            now_loop = time.monotonic()
            gap = now_loop - previous_iteration
            previous_iteration = now_loop
            if gap > STALL_THRESHOLD:
                self._result.stalls += 1
                self._result.max_stall = max(self._result.max_stall, gap)
                _log.warning("UI loop stalled %.2fs (slowest redraw %.0f ms, clock pos %.1f)",
                             gap, self._result.max_render_ms, clock.position())
            if self._handle_keys():
                dirty = True
            if self._result.quit_by_user or clock.error or clock.ended:
                self._result.completed = clock.ended and not clock.error \
                    and not self._result.quit_by_user
                break
            pos = clock.position()
            if self._bg is not None and self._state.show_video:
                got = self._bg.take()
                if got is not None:
                    self._raw_frame = got[1]
                    self._ascii_dirty = True
                    dirty = True
            lyric_idx = self._lyrics.index_at(pos) if self._state.show_lyrics else -1
            if lyric_idx != last_lyric or (self._lyrics.ready and not self._lyrics.is_synced
                                           and not self._state.lyrics_message):
                last_lyric = lyric_idx
                dirty = True
            tick = int(pos * 4)
            if tick != last_second_tick:
                last_second_tick = tick
                dirty = True
            if self._console.size[:2] != self._size:
                dirty = True
            now = time.monotonic()
            # Video follows the audio clock, so skipping redraws never desynchronises anything;
            # it only keeps a slow terminal from blocking us (and freezing the picture).
            interval = max(min_interval, self._render_ema * ADAPTIVE_FACTOR)
            if dirty and now - last_render >= interval:
                self._state.position = pos
                self._state.duration = clock.duration or self._state.duration
                self._state.paused = self._audio.paused
                self._state.volume = self._audio.volume
                self._state.lyrics = self._lyrics.window_at(pos)
                self._state.lyrics_message = self._lyrics.message
                self._render()
                last_render = now
                dirty = False
            time.sleep(IDLE_TICK)

    def _handle_keys(self) -> bool:
        changed = False
        for key in self._keys.poll():
            changed = True
            if key is Key.QUIT:
                self._result.quit_by_user = True
            elif key is Key.PAUSE:
                self._audio.toggle_pause()
            elif key is Key.SEEK_BACK:
                self._audio.seek_backward()
            elif key is Key.SEEK_FORWARD:
                self._audio.seek_forward()
            elif key is Key.VOLUME_UP:
                self._audio.volume_up()
            elif key is Key.VOLUME_DOWN:
                self._audio.volume_down()
            elif key is Key.TOGGLE_LYRICS:
                self._state.show_lyrics = not self._state.show_lyrics
                self._size = (0, 0)  # video area changed: rebuild converter
            elif key is Key.TOGGLE_VIDEO:
                self._state.show_video = not self._state.show_video
        return changed

