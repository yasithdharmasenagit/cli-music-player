"""Playback session: wires audio clock, ASCII video, lyrics, keyboard and the Rich screen."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from rich.console import Console
from rich.live import Live

from src.audio.audio_controller import AudioController
from src.core.config import AppConfig, Quality
from src.core.exceptions import VideoLoadError
from src.lyrics.lyrics_models import Lyrics, LyricsStatus
from src.lyrics.lyrics_sync import LyricsTrack
from src.media.media_resolver import ResolvedMedia
from src.ui.keyboard import Key, KeyReader
from src.ui.player_view import ViewState, build_loading_view, build_player_view, video_area
from src.utils.logger import get_logger
from src.utils.stderr import suppress_native_stderr
from src.utils.timer import high_resolution_timer
from src.video.ascii_converter import ASCIIConverter
from src.video.video_loader import VideoLoader
from src.video.video_sync import BackgroundVideoSource, SyncedVideoSource

_log = get_logger("session")

LOAD_TIMEOUT = 15.0      # seconds to wait for mpv to report the first position
STALL_THRESHOLD = 0.5    # seconds without a UI loop iteration counts as a stall
ADAPTIVE_FACTOR = 3.0    # redraw at most every 3x the measured redraw cost (<= ~33% of the time)
OTHER_REDRAW_INTERVAL = 0.05  # redraws not caused by a new video frame (clock, lyrics, notices)
K_RELAX_SECONDS = 3.0    # how long the terminal must keep up before showing more frames again
MAX_IDLE_WAIT = 0.02     # longest the UI loop sleeps; bounds key-press latency to 20 ms
SIZE_CHECK_INTERVAL = 0.15  # how often the terminal size is polled (resizes appear within this)
NOTICE_SECONDS = 2.5


@dataclass(slots=True)
class SessionResult:
    """How a session ended, plus performance statistics."""

    quit_by_user: bool = False
    completed: bool = False
    error: str | None = None
    notice: str | None = None
    frames_decoded: int = 0
    frames_delivered: int = 0  # frames that reached the screen (decoded-ahead ones excluded)
    frames_skipped: int = 0
    seeks: int = 0
    duration_played: float = 0.0
    #: Diagnostics: loop iterations that took longer than STALL_THRESHOLD, and worst timings.
    stalls: int = 0
    max_stall: float = 0.0
    max_render_ms: float = 0.0
    max_clock_gap: float = 0.0
    video_recoveries: int = 0
    # Performance statistics (also written to the log).
    redraws: int = 0
    avg_render_ms: float = 0.0
    avg_convert_ms: float = 0.0
    cpu_percent: float = 0.0  # CPU time of this process (all threads) / wall time, one core = 100


class PlaybackSession:
    """Runs one track until it ends, errors, or the user quits.

    Position always comes from ``audio.clock`` (mpv). The loop merely polls it: a video frame
    is decoded (on a background thread) when the clock says a new one is due, lyrics are looked
    up by clock time, and the screen is redrawn only when something visible changed. A session
    object plays exactly once.
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
            quality=config.quality.label, compact=config.compact,
        )
        self._loader: VideoLoader | None = None
        self._source: SyncedVideoSource | None = None
        self._bg: BackgroundVideoSource | None = None
        self._converter: ASCIIConverter | None = None
        self._raw_frame: np.ndarray | None = None
        self._size: tuple[int, int] = (0, 0)
        self._term_size: tuple[int, int] = (0, 0)
        self._last_size_check = 0.0
        self._ascii_dirty = False
        self._result = SessionResult()
        self._live: Live | None = None
        self._render_ema = 0.0  # seconds
        self._render_total = 0.0
        self._convert_total = 0.0
        self._notice_until = 0.0
        self._lyrics_notified = False
        self._was_recovering = False
        self._started = False
        # Even frame cadence: show every k-th video frame (k whole number) so the spacing between
        # shown frames is constant. A fixed redraw rate that does not divide the video's frame
        # rate (24 vs 25/30/50/60 fps) shows frames unevenly, which looks like judder.
        self._k = 1
        self._k_cap = 1
        self._k_changed = 0.0
        self._last_shown = -1

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
            self._state.video_notice = "Video could not be decoded: playing audio only"
            self._result.notice = "Video could not be decoded; playing audio only."
            return
        self._loader = loader
        self._source = SyncedVideoSource(loader)
        self._bg = BackgroundVideoSource(self._source, self._audio.clock.position,
                                         pause=lambda: not self._state.show_video)

    def _rebuild_converter(self, size: tuple[int, int]) -> None:
        cols, rows = video_area(size[0], size[1], self._state.show_lyrics, self._config.compact)
        self._converter = ASCIIConverter(
            width=min(self._config.effective_width, cols), charset=self._config.active_charset,
            invert=self._config.invert, color=self._config.color,
            char_aspect=self._config.char_aspect, max_rows=rows,
            interpolation=self._config.interpolation, stabilize=self._config.stabilize,
            chunk=self._config.chunk, enhance=self._config.enhance)
        self._ascii_dirty = self._raw_frame is not None

    def _notify(self, text: str, kind: str = "info", seconds: float = NOTICE_SECONDS) -> None:
        self._state.notice = text
        self._state.notice_kind = kind
        self._notice_until = time.monotonic() + seconds

    # -- main entry

    def run(self) -> SessionResult:
        """Play the media. Always releases mpv, the decoder thread, the video file and the terminal.

        Raises:
            RuntimeError: if this session was already run.
            AudioError: if mpv cannot be started (raised before the screen is taken over).
        """
        if self._started:
            raise RuntimeError("A PlaybackSession can only be run once; create a new one.")
        self._started = True
        _log.info("Session start: %s (%s, video=%s, quality=%s)", self._media.metadata.display,
                  self._media.origin, self._media.has_video, self._config.quality.name)
        cpu0, wall0 = time.process_time(), time.perf_counter()
        try:
            self._open_video()
            self._audio.load(self._media.audio_source, self._media.user_agent)  # may raise AudioError
            self._lyrics.start()
            with self._keys, suppress_native_stderr(), high_resolution_timer(), Live(
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
        self._finish(cpu0, wall0)
        return self._result

    def _finish(self, cpu0: float, wall0: float) -> None:
        r = self._result
        r.duration_played = self._audio.position
        r.max_clock_gap = self._audio.clock.max_update_gap
        if self._source is not None:
            r.frames_decoded = self._source.frames_decoded
            r.frames_delivered = self._bg.delivered if self._bg is not None else 0
            r.frames_skipped = self._source.frames_skipped
            r.seeks = self._source.seeks
            r.video_recoveries = self._source.recoveries
        if r.redraws:
            r.avg_render_ms = self._render_total / r.redraws * 1000
            r.avg_convert_ms = self._convert_total / r.redraws * 1000
        wall = time.perf_counter() - wall0
        r.cpu_percent = 100.0 * (time.process_time() - cpu0) / wall if wall > 0 else 0.0
        if self._audio.error and not r.quit_by_user:
            r.error = self._audio.error
        outcome = "error" if r.error else "user quit" if r.quit_by_user else "completed"
        _log.info(
            "Session end (%s): played %.1fs | decoded %d (shown %d), skipped %d, seeks %d, recoveries %d | "
            "redraws %d, render avg %.1f ms (max %.1f), convert avg %.2f ms | stalls %d (max %.2fs) "
            "| mpv clock gap max %.0f ms | cpu %.0f%% of one core",
            outcome, r.duration_played, r.frames_decoded, r.frames_delivered, r.frames_skipped, r.seeks,
            r.video_recoveries, r.redraws, r.avg_render_ms, r.max_render_ms, r.avg_convert_ms,
            r.stalls, r.max_stall, r.max_clock_gap * 1000, r.cpu_percent)
        if r.error:
            _log.error("Session error: %s", r.error)

    # -- drawing

    def _terminal_size(self, force: bool = False) -> tuple[int, int]:
        """Terminal size, polled at most every SIZE_CHECK_INTERVAL (querying the OS is not free)."""
        now = time.monotonic()
        if force or self._term_size == (0, 0) or now - self._last_size_check >= SIZE_CHECK_INTERVAL:
            size = self._console.size
            self._term_size = (size.width, size.height)
            self._last_size_check = now
        return self._term_size

    def _render(self, loading: bool = False) -> None:
        size = self._terminal_size()
        if not loading:
            if size != self._size or self._converter is None:
                self._size = size
                self._rebuild_converter(size)
            if self._ascii_dirty and self._raw_frame is not None and self._converter is not None:
                t0 = time.perf_counter()
                try:
                    self._state.ascii_frame = self._converter.convert(self._raw_frame)
                except Exception as exc:  # bad frame: keep the previous picture
                    _log.warning("Frame conversion failed: %s", type(exc).__name__)
                self._convert_total += time.perf_counter() - t0
                self._ascii_dirty = False
        if self._live is None:
            return
        view = build_loading_view(self._state) if loading else build_player_view(self._state, size[0])
        t0 = time.perf_counter()
        self._live.update(view, refresh=True)
        took = time.perf_counter() - t0
        if not loading:
            self._result.redraws += 1
            self._render_total += took
            self._result.max_render_ms = max(self._result.max_render_ms, took * 1000)
            # Smoothed redraw cost, used to throttle redraws if the terminal is slow.
            self._render_ema = took if self._render_ema == 0 else 0.8 * self._render_ema + 0.2 * took

    def _set_step(self, label: str, status: str) -> None:
        steps = [(lbl, st) for lbl, st in self._state.loading_steps if lbl != label]
        self._state.loading_steps = steps + [(label, status)]

    def _wait_until_ready(self) -> bool:
        """Show the loading screen until mpv reports its first position."""
        deadline = time.monotonic() + LOAD_TIMEOUT
        self._set_step("Media information", "done")
        if self._media.has_video:
            self._set_step("Video stream", "done" if self._source is not None else "fail")
        self._set_step("Audio engine", "active")
        if self._lyrics.ready:
            self._set_step("Lyrics", "skip")
        else:
            self._set_step("Lyrics", "active")
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
            self._state.spinner += 1
            self._refresh_lyrics_step()
            self._render(loading=True)
            time.sleep(0.08)
        self._set_step("Audio engine", "done")
        self._refresh_lyrics_step()
        if self._source is not None:
            first = self._source.frame_at(0.0)
            if first is not None:
                self._raw_frame = first[1]
                self._ascii_dirty = True
        return True

    def _refresh_lyrics_step(self) -> None:
        if not self._lyrics.ready:
            return
        lyrics = self._lyrics.lyrics
        if lyrics is None or lyrics.status is LyricsStatus.DISABLED:
            self._set_step("Lyrics", "skip")
        elif lyrics.is_synced:
            self._set_step("Lyrics", "done")
        else:
            self._set_step("Lyrics", "fail")

    # -- main loop

    def _accept_frame(self, index: int, k_cap: int) -> bool:
        """Decide whether the frame delivered by the decoder is one of the every-k-th frames to show.

        ``k_cap`` is the smallest whole number keeping the redraw rate within the configured
        maximum. If the terminal cannot keep up, k grows (still a whole number, so the cadence
        stays even) and relaxes again once it has kept up for a few seconds.
        """
        now = time.monotonic()
        fps = self._source.fps if self._source is not None else 0.0
        need = math.ceil(self._render_ema * ADAPTIVE_FACTOR * fps - 1e-6) if self._render_ema and fps else 1
        want = max(k_cap, need)
        if want > self._k:
            self._k, self._k_changed = want, now
        elif want < self._k and now - self._k_changed > K_RELAX_SECONDS:
            self._k, self._k_changed = self._k - 1, now
        last = self._last_shown
        if last < 0 or index < last or index >= last + self._k:  # first frame, a seek back, or due
            self._last_shown = index
            return True
        return False

    def _compute_k_cap(self) -> int:
        """Smallest whole-number frame step that keeps redraws within the configured maximum."""
        fps = self._source.fps if self._source is not None else 0.0
        return max(1, math.ceil(fps / self._config.effective_ui_fps - 0.02)) if fps else 1

    def _set_quality(self, quality: Quality) -> None:
        """Switch quality profile while playing (keys 1/2/3).

        Everything the profile controls (character ramp, maximum width, resize filter, redraw
        cap) is re-derived; custom settings (``--charset``, ``--width``, ``--ui-fps``) still win.
        """
        if quality is self._config.quality:
            self._notify(f"Quality: {quality.label}")
            return
        self._config = self._config.with_overrides(quality=quality)
        self._state.quality = quality.label
        self._state.compact = self._config.compact
        self._size = (0, 0)  # forces a new converter on the next redraw
        self._k_cap = self._compute_k_cap()
        self._k, self._k_changed = self._k_cap, time.monotonic()
        self._notify(f"Quality: {quality.label}")
        _log.info("Quality switched to %s", quality.label)

    def _loop(self) -> None:
        clock = self._audio.clock
        self._k_cap = self._compute_k_cap()
        self._k = self._k_cap
        last_render = 0.0
        last_second_tick = -1
        last_lyric = -2
        dirty = True
        previous_iteration = time.monotonic()
        while True:
            frame_due = False
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
                if got is not None and self._accept_frame(got[0], self._k_cap):
                    self._raw_frame = got[1]
                    self._ascii_dirty = True
                    dirty = True
                    frame_due = True
            lyric_idx = self._lyrics.index_at(pos) if self._state.show_lyrics else -1
            if lyric_idx != last_lyric or (self._lyrics.ready and not self._lyrics.is_synced
                                           and not self._state.lyrics_message):
                last_lyric = lyric_idx
                dirty = True
            tick = int(pos * 4)
            if tick != last_second_tick:
                last_second_tick = tick
                dirty = True
            if self._update_notices():
                dirty = True
            if self._terminal_size() != self._size:
                dirty = True
            now = time.monotonic()
            # Video follows the audio clock, so skipping redraws never desynchronises anything;
            # it only keeps a slow terminal from blocking us (and freezing the picture).
            slow = max(OTHER_REDRAW_INTERVAL, self._render_ema * ADAPTIVE_FACTOR)
            if frame_due or (dirty and now - last_render >= slow):
                self._state.position = pos
                self._state.duration = clock.duration or self._state.duration
                self._state.paused = self._audio.paused
                self._state.volume = self._audio.volume
                self._state.lyrics = self._lyrics.window_at(pos)
                self._state.lyrics_message = self._lyrics.message
                self._render()
                last_render = now
                dirty = False
            self._idle(dirty, last_render + slow)

    def _idle(self, redraw_pending: bool, next_redraw_at: float) -> None:
        """Sleep until something can change: a new frame, a key press or the next allowed redraw.

        Waiting on the decoder's "frame ready" event (instead of polling every few ms) keeps
        the idle CPU cost low while key presses are still noticed within MAX_IDLE_WAIT.
        """
        timeout = MAX_IDLE_WAIT
        if redraw_pending:
            timeout = min(timeout, max(0.001, next_redraw_at - time.monotonic()))
        if self._bg is not None and self._state.show_video:
            self._bg.wait_for_frame(timeout)
        else:
            time.sleep(timeout)

    def _update_notices(self) -> bool:
        """Maintain transient notifications. Returns True if the visible notice changed."""
        changed = False
        now = time.monotonic()
        recovering = self._source is not None and self._source.recovering
        if recovering != self._was_recovering:
            self._was_recovering = recovering
            if recovering:
                self._notify("Video stream interrupted; reconnecting…", "warn", seconds=60.0)
            else:
                self._notify("Video reconnected", "info")
            changed = True
        if not self._lyrics_notified and self._lyrics.ready:
            self._lyrics_notified = True
            lyrics = self._lyrics.lyrics
            if lyrics is not None and lyrics.status is LyricsStatus.ERROR:
                self._notify("Lyrics service unavailable", "warn", seconds=4.0)
                changed = True
        if self._state.notice and now >= self._notice_until:
            self._state.notice = ""
            changed = True
        return changed

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
                self._notify(f"◀◀ {self._config.seek_step:g}s")
            elif key is Key.SEEK_FORWARD:
                self._audio.seek_forward()
                self._notify(f"{self._config.seek_step:g}s ▶▶")
            elif key is Key.VOLUME_UP:
                self._audio.volume_up()
            elif key is Key.VOLUME_DOWN:
                self._audio.volume_down()
            elif key is Key.TOGGLE_LYRICS:
                self._state.show_lyrics = not self._state.show_lyrics
                self._size = (0, 0)  # video area changed: rebuild converter
            elif key is Key.TOGGLE_VIDEO:
                self._state.show_video = not self._state.show_video
            elif key is Key.QUALITY_LOW:
                self._set_quality(Quality.LOW)
            elif key is Key.QUALITY_MEDIUM:
                self._set_quality(Quality.BALANCED)
            elif key is Key.QUALITY_HIGH:
                self._set_quality(Quality.HIGH)
        return changed
