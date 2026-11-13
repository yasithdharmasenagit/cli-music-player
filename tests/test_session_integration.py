"""Integration: PlaybackSession + AudioController + clock + real video file + lyrics + keys.

mpv is replaced by FakeMpv, which feeds the shared PlaybackClock like the real IPC events do.
"""

from __future__ import annotations

import io
import threading
import time

import pytest
from rich.console import Console

from src.audio.audio_controller import AudioController
from src.audio.playback_clock import PlaybackClock
from src.core.config import AppConfig
from src.core.exceptions import AudioPlaybackError, VideoLoadError
from src.lyrics.lyrics_models import LyricLine, Lyrics, LyricsStatus
from src.media.media_resolver import MediaResolver
from src.media.metadata import MediaKind, MediaMetadata
from src.player.session import PlaybackSession
from src.ui.keyboard import KeyReader


class FakeMpv:
    """Audio backend that advances the clock in real time while unpaused."""

    def __init__(self, clock: PlaybackClock, duration: float, fail_start: bool = False):
        self.clock, self.duration, self.fail_start = clock, duration, fail_start
        self.calls: list[tuple] = []
        self._pos = 0.0
        self._paused = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stopped = False

    def start(self, source, volume, user_agent=None):
        if self.fail_start:
            raise AudioPlaybackError("mpv exploded")
        self.calls.append(("start", source))
        self.clock.update(position=0.0, paused=True, duration=self.duration)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        last = time.monotonic()
        while not self._stop.is_set():
            time.sleep(0.01)
            now = time.monotonic()
            if not self._paused:
                self._pos += now - last
                if self._pos >= self.duration:
                    self.clock.update(position=self.duration)
                    self.clock.mark_ended()
                    return
                self.clock.update(position=self._pos)
            last = now

    def set_pause(self, paused):
        self.calls.append(("pause", paused))
        self._paused = paused
        self.clock.update(paused=paused)

    def seek_relative(self, s):
        self.calls.append(("seek", s))
        self._pos = max(0.0, min(self.duration, self._pos + s))
        self.clock.update(position=self._pos)

    def seek_absolute(self, s):
        self._pos = s
        self.clock.update(position=s)

    def set_volume(self, v):
        self.calls.append(("volume", v))

    def stop(self):
        self.stopped = True
        self._stop.set()


class ScriptedKeys:
    """Delivers characters at given offsets (seconds after the first poll)."""

    def __init__(self, script):
        self.script = sorted(script, key=lambda item: item[0])
        self.t0 = None

    def has_char(self):
        if self.t0 is None:
            self.t0 = time.monotonic()
        return bool(self.script) and time.monotonic() - self.t0 >= self.script[0][0]

    def get_char(self):
        return self.script.pop(0)[1]


def console_and_buffer():
    buf = io.StringIO()
    return Console(file=buf, force_terminal=True, width=100, height=32, color_system=None), buf


def build(media_kind="video", video_path=None, *, duration=1.5, script=(), lyrics=None,
          config=None, fail_start=False, loader_factory=None):
    clock = PlaybackClock()
    mpv = FakeMpv(clock, duration, fail_start)
    audio = AudioController(mpv, clock, volume=80)
    console, buf = console_and_buffer()
    meta = MediaMetadata("Test Song", "Test Band", duration)
    from src.media.media_resolver import ResolvedMedia
    media = ResolvedMedia(meta, MediaKind.VIDEO if media_kind == "video" else MediaKind.AUDIO,
                          "audio-src", str(video_path) if media_kind == "video" else None,
                          "Local file")
    cfg = config or AppConfig(ascii_width=40, alt_screen=False, ui_fps=60.0)
    kwargs = {"loader_factory": loader_factory} if loader_factory else {}
    session = PlaybackSession(media, cfg, audio, (lambda: lyrics) if lyrics is not None else None,
                              console, KeyReader(ScriptedKeys(script), windows=True), **kwargs)
    return session, mpv, buf


def test_full_playback_video_audio_lyrics(make_video):
    video = make_video(frames=30, fps=20.0)  # 1.5 s
    lyrics = Lyrics(LyricsStatus.SYNCED, (LyricLine(0.1, "FIRST LYRIC"), LyricLine(0.6, "SECOND LYRIC")))
    session, mpv, buf = build("video", video, lyrics=lyrics)
    result = session.run()
    out = buf.getvalue()
    assert result.completed and not result.quit_by_user and result.error is None
    assert result.frames_decoded >= 5
    assert mpv.stopped and ("pause", False) in mpv.calls  # audio started after video was ready
    assert "FIRST LYRIC" in out and "SECOND LYRIC" in out
    assert out.index("FIRST LYRIC") < out.index("SECOND LYRIC")
    assert "NOW PLAYING" in out and "Test Song" in out and "Test Band" in out
    assert "00:01 / 00:01" in out or "/ 00:01" in out


def test_video_follows_audio_clock_not_wall_time(make_video):
    """A paused clock must freeze the picture: no frames are decoded while paused."""
    video = make_video(frames=60, fps=20.0)
    session, mpv, buf = build("video", video, duration=3.0, script=[(0.4, " "), (1.3, "q")])
    result = session.run()
    assert result.quit_by_user
    # ~0.4s of playback at 20 fps = ~8 frames, then paused for 0.9 s (would be ~18 more frames
    # if video ran on its own clock)
    assert 2 <= result.frames_decoded + result.frames_skipped <= 14
    assert ("pause", True) in mpv.calls


def test_keyboard_controls_reach_audio_engine(make_video):
    video = make_video(frames=60, fps=20.0)
    script = [(0.2, "\xe0"), (0.2, "M"),   # right: seek +5
              (0.3, "\xe0"), (0.3, "K"),   # left: seek -5
              (0.4, "\xe0"), (0.4, "H"),   # up: volume +5
              (0.5, "\xe0"), (0.5, "P"),   # down: volume -5
              (0.6, "l"), (0.7, "v"), (0.8, "L"), (0.9, "V"),
              (1.0, "q")]
    session, mpv, buf = build("video", video, duration=30.0, script=script)
    result = session.run()
    assert result.quit_by_user
    assert ("seek", 5.0) in mpv.calls and ("seek", -5.0) in mpv.calls
    assert ("volume", 85) in mpv.calls and ("volume", 80) in mpv.calls
    assert result.seeks >= 1  # the +5 s jump made the video seek to follow the clock


def test_quit_during_loading_and_cleanup(make_video):
    session, mpv, _ = build("video", make_video(frames=10), script=[(0.0, "q")])
    result = session.run()
    assert result.quit_by_user and mpv.stopped


def test_audio_only_session(tmp_path):
    session, mpv, buf = build("audio", duration=0.6)
    result = session.run()
    assert result.completed and result.frames_decoded == 0
    assert "Audio only" in buf.getvalue()


@pytest.mark.parametrize("lyrics,expected", [
    (Lyrics(LyricsStatus.NOT_FOUND), "No lyrics found."),
    (Lyrics(LyricsStatus.INSTRUMENTAL), "Instrumental"),
    (Lyrics(LyricsStatus.UNSYNCED, message="Lyrics exist but are not time-synchronized, so they are not shown."),
     "not time-synchronized"),
    (Lyrics(LyricsStatus.ERROR, message="Lyrics lookup timed out."), "timed out"),
])
def test_missing_lyrics_are_reported_not_invented(lyrics, expected):
    session, _, buf = build("audio", duration=0.6, lyrics=lyrics)
    assert session.run().completed
    assert expected in buf.getvalue()


def test_lyrics_disabled():
    cfg = AppConfig(ascii_width=40, alt_screen=False, lyrics_enabled=False)
    session, _, buf = build("audio", duration=0.4, config=cfg, lyrics=Lyrics(LyricsStatus.SYNCED, (LyricLine(0, "X"),)))
    assert session.run().completed


def test_lyrics_lookup_does_not_block_playback():
    release = threading.Event()

    def slow():
        release.wait(5)
        return Lyrics(LyricsStatus.NOT_FOUND)

    session, mpv, buf = build("audio", duration=0.5)
    session._lyrics = __import__("src.lyrics.lyrics_sync", fromlist=["LyricsTrack"]).LyricsTrack(slow)
    t0 = time.monotonic()
    result = session.run()
    release.set()
    assert result.completed and time.monotonic() - t0 < 3
    assert "Searching for lyrics" in buf.getvalue()


def test_audio_error_during_playback_is_reported(make_video):
    session, mpv, _ = build("video", make_video(frames=30, fps=20.0), duration=5.0)
    threading.Timer(0.5, lambda: session._audio.clock.set_error("Audio playback failed: boom")).start()
    result = session.run()
    assert result.error == "Audio playback failed: boom" and mpv.stopped


def test_mpv_start_failure_propagates_and_cleans_up(make_video):
    session, mpv, _ = build("video", make_video(frames=5), fail_start=True)
    with pytest.raises(AudioPlaybackError, match="exploded"):
        session.run()


def test_video_decode_error_falls_back_to_audio_only(make_video):
    def bad_loader(src):
        raise VideoLoadError("cannot decode")

    session, _, buf = build("video", make_video(frames=5), duration=0.5, loader_factory=bad_loader)
    result = session.run()
    assert result.completed and result.notice and "audio only" in result.notice
    assert "audio only" in buf.getvalue().lower()


def test_loader_is_released_after_session(make_video):
    loaders = []
    from src.video.video_loader import VideoLoader

    def factory(src):
        loaders.append(VideoLoader(src))
        return loaders[-1]

    session, _, _ = build("video", make_video(frames=10, fps=20.0), duration=0.4, loader_factory=factory)
    session.run()
    assert loaders and not loaders[0].is_open


def test_end_to_end_local_file_resolution_then_session(make_video):
    path = make_video("clip.avi", frames=10, fps=10.0)
    media = MediaResolver(probe=lambda p: None).resolve(str(path))
    clock = PlaybackClock()
    mpv = FakeMpv(clock, media.metadata.duration or 1.0)
    console, buf = console_and_buffer()
    session = PlaybackSession(media, AppConfig(ascii_width=30, alt_screen=False), AudioController(mpv, clock),
                              None, console, KeyReader(ScriptedKeys([]), windows=True))
    assert session.run().completed
    assert ("start", str(path)) in mpv.calls


def test_slow_terminal_throttles_redraws_but_stays_in_sync(make_video):
    """A terminal that takes 120 ms per redraw must not freeze or desync the session."""
    video = make_video(frames=60, fps=20.0)  # 3 s
    session, mpv, buf = build("video", video, duration=3.0)
    real_update = None
    import src.player.session as sess_mod

    class SlowLive:
        def __init__(self, inner):
            self.inner, self.updates = inner, 0

        def update(self, view, refresh=False):
            self.updates += 1
            time.sleep(0.12)
            self.inner.update(view, refresh=refresh)

    orig_render = session._render

    def wrapped():
        if not isinstance(session._live, SlowLive) and session._live is not None:
            session._live = SlowLive(session._live)
        orig_render()

    session._render = wrapped
    t0 = time.monotonic()
    result = session.run()
    elapsed = time.monotonic() - t0
    assert result.completed and elapsed < 5.0           # finished on audio time, not video time
    assert result.max_stall < 0.5                        # loop never blocked for long
    assert session._live.updates < 30                    # throttled well below 24 fps
    assert result.frames_skipped + result.frames_decoded <= 61
    del real_update, sess_mod
