"""Resource cleanup, error recovery and lifecycle guarantees of a playback session."""

from __future__ import annotations

import io
import logging
import os
import threading
import time

import httpx
import pytest
from rich.console import Console

from src.audio.audio_controller import AudioController
from src.audio.audio_player import MpvBackend
from src.audio.playback_clock import PlaybackClock
from src.cli import main as cli
from src.core.config import AppConfig
from src.core.exceptions import AudioPlaybackError
from src.lyrics.lyrics_client import LrclibProvider, LyricsCache, LyricsQuery
from src.lyrics.lyrics_models import LyricLine, Lyrics, LyricsStatus
from src.media.media_resolver import ResolvedMedia
from src.media.metadata import MediaKind, MediaMetadata
from src.player.session import PlaybackSession
from src.ui.keyboard import KeyReader
from src.utils.logger import configure_logging
from src.video.video_loader import VideoLoader
from tests.test_audio import FakeProc, FakeTransport
from tests.test_session_integration import FakeMpv, ScriptedKeys
from tests.test_stream_recovery import flaky_factory

WORKER_NAMES = {"video-decode", "mpv-ipc", "lyrics-fetch"}


def workers_alive():
    return [t.name for t in threading.enumerate() if t.name in WORKER_NAMES and t.is_alive()]


def assert_workers_stop(timeout=2.0):
    end = time.time() + timeout
    while time.time() < end and workers_alive():
        time.sleep(0.01)
    assert workers_alive() == []


def make_session(video_path=None, *, duration=1.0, script=(), lyrics=None, config=None, mpv=None,
                 keys=None, console=None, loader_factory=None, kind=None):
    clock = PlaybackClock()
    mpv = mpv or FakeMpv(clock, duration)
    if hasattr(mpv, "clock"):
        clock = mpv.clock
    audio = AudioController(mpv, clock)
    buf = io.StringIO()
    console = console or Console(file=buf, force_terminal=True, width=100, height=30, color_system=None)
    meta = MediaMetadata("Song", "Band", duration)
    media = ResolvedMedia(meta, MediaKind.VIDEO if video_path else MediaKind.AUDIO, "audio-src",
                          str(video_path) if video_path else None, "Local file")
    cfg = config or AppConfig(ascii_width=40, alt_screen=False)
    extra = {"loader_factory": loader_factory} if loader_factory else {}
    session = PlaybackSession(media, cfg, audio, (lambda: lyrics) if lyrics is not None else None,
                              console, keys or KeyReader(ScriptedKeys(script), windows=True), **extra)
    return session, mpv, buf


# ------------------------------------------------------------------ single use / cleanup


def test_a_session_can_only_run_once(make_video):
    session, *_ = make_session(duration=0.3)
    assert session.run().completed
    with pytest.raises(RuntimeError, match="only be run once"):
        session.run()


def test_no_worker_threads_survive_any_ending(make_video):
    video = make_video(frames=40, fps=20.0)
    lyr = Lyrics(LyricsStatus.SYNCED, (LyricLine(0.1, "x"),))
    for kwargs in ({"duration": 0.6},                                    # natural end
                   {"duration": 5.0, "script": [(0.3, "q")]},            # user quit
                   {"duration": 0.6, "lyrics": lyr}):                    # with lyrics thread
        session, mpv, _ = make_session(video, **kwargs)
        session.run()
        assert mpv.stopped
    assert_workers_stop()


def test_repeated_play_requests_do_not_accumulate_resources(make_video):
    video = make_video(frames=30, fps=20.0)
    baseline = threading.active_count()
    for _ in range(4):
        session, mpv, _ = make_session(video, duration=0.5)
        assert session.run().completed and mpv.stopped
    assert_workers_stop()
    deadline = time.time() + 2
    while threading.active_count() > baseline and time.time() < deadline:
        time.sleep(0.02)
    assert threading.active_count() <= baseline


def test_video_file_is_released_after_every_ending(make_video):
    video = make_video(frames=30, fps=20.0)
    loaders = []

    def factory(src):
        loaders.append(VideoLoader(src))
        return loaders[-1]

    for kwargs in ({"duration": 0.4}, {"duration": 5.0, "script": [(0.2, "q")]}):
        make_session(video, loader_factory=factory, **kwargs)[0].run()
    assert len(loaders) == 2 and not any(loader.is_open for loader in loaders)


# ------------------------------------------------------------------ audio engine failures


def real_backend_session(video=None, duration=30.0):
    clock = PlaybackClock()
    transport, proc = FakeTransport(), FakeProc()
    backend = MpvBackend(clock, popen=lambda a, **k: proc, connect=lambda addr, alive: transport,
                         find=lambda c: "mpv-fake")
    session, _, buf = make_session(video, mpv=backend, duration=duration)
    session._audio = AudioController(backend, clock)
    return session, backend, transport, proc, clock, buf


def test_unexpected_mpv_termination_ends_the_session_cleanly(make_video):
    session, backend, transport, proc, clock, buf = real_backend_session(make_video(frames=40, fps=20.0))

    def feed_then_die():
        time.sleep(0.15)
        transport.push_event(event="property-change", name="duration", data=30.0)
        transport.push_event(event="property-change", name="time-pos", data=0.0)
        transport.push_event(event="property-change", name="pause", data=False)
        time.sleep(0.4)
        proc.returncode = 7
        transport.incoming.put(None)  # the pipe closes because mpv died

    threading.Thread(target=feed_then_die, daemon=True).start()
    t0 = time.monotonic()
    result = session.run()
    assert time.monotonic() - t0 < 5
    assert result.error and "unexpectedly" in result.error and "7" in result.error
    assert not result.completed and transport.closed
    assert_workers_stop()


def test_mpv_that_never_reports_a_position_times_out(make_video, monkeypatch):
    import src.player.session as session_module

    monkeypatch.setattr(session_module, "LOAD_TIMEOUT", 0.4)
    session, backend, transport, proc, clock, buf = real_backend_session()
    result = session.run()
    assert result.error and "Timed out" in result.error and transport.closed
    assert "Preparing" in buf.getvalue() or "Audio engine" in buf.getvalue()  # loading screen was shown


def test_mpv_start_failure_leaves_nothing_behind(make_video):
    session, mpv, _ = make_session(make_video(frames=10, fps=10.0), duration=1.0)
    mpv.fail_start = True
    with pytest.raises(AudioPlaybackError):
        session.run()
    assert_workers_stop()


# ------------------------------------------------------------------ interruption


def test_ctrl_c_inside_the_loop_is_a_clean_quit(make_video, capfd):
    class Raising:
        calls = 0

        def has_char(self):
            self.calls += 1
            if self.calls > 40:
                raise KeyboardInterrupt
            return False

        def get_char(self):
            return ""

    session, mpv, _ = make_session(make_video(frames=40, fps=20.0), duration=30.0,
                                   keys=KeyReader(Raising(), windows=True))
    result = session.run()
    assert result.quit_by_user and not result.error and mpv.stopped
    os.write(2, b"stderr is visible again\n")  # native stderr must be restored after the session
    assert "stderr is visible again" in capfd.readouterr().err
    assert_workers_stop()


def test_quit_while_loading_is_clean():
    class SlowStart(FakeMpv):
        def start(self, source, volume, user_agent=None):
            pass  # never reports a position: stays on the loading screen

    clock = PlaybackClock()
    session, mpv, buf = make_session(duration=5.0, mpv=SlowStart(clock, 5.0), script=[(0.2, "q")])
    result = session.run()
    assert result.quit_by_user and mpv.stopped


# ------------------------------------------------------------------ terminal


def test_alternate_screen_and_cursor_are_restored(make_video):
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=100, height=30, color_system=None,
                      legacy_windows=False)  # behave like Windows Terminal (VT sequences)
    session, *_ = make_session(make_video(frames=20, fps=20.0), duration=0.5, console=console,
                               config=AppConfig(ascii_width=40, alt_screen=True))
    assert session.run().completed
    out = buf.getvalue()
    assert out.index("\x1b[?1049h") < out.rindex("\x1b[?1049l")  # entered, then left
    assert out.rindex("\x1b[?25h") > out.index("\x1b[?25l")        # cursor hidden, then shown again


def test_terminal_restored_even_when_the_session_dies(make_video):
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=100, height=30, color_system=None,
                      legacy_windows=False)
    session, mpv, _ = make_session(make_video(frames=20, fps=20.0), duration=10.0, console=console,
                                   config=AppConfig(ascii_width=40, alt_screen=True))
    threading.Timer(0.4, lambda: session._audio.clock.set_error("Audio playback failed: boom")).start()
    assert session.run().error == "Audio playback failed: boom"
    out = buf.getvalue()
    assert out.rindex("\x1b[?1049l") > out.index("\x1b[?1049h") and "\x1b[?25h" in out


def test_terminal_resize_during_playback_refits_the_picture(make_video):
    console = Console(file=io.StringIO(), force_terminal=True, width=120, height=40, color_system=None,
                      legacy_windows=False)
    session, mpv, _ = make_session(make_video(frames=80, fps=20.0), duration=2.0, console=console,
                                   config=AppConfig(ascii_width=100, alt_screen=False))
    widths = []

    def shrink():
        time.sleep(0.5)
        widths.append(session._converter.processor.width)
        console.size = (60, 24)

    threading.Thread(target=shrink, daemon=True).start()
    assert session.run().completed
    assert widths[0] == 100 and session._converter.processor.width <= 58  # refitted to the new size
    assert session._size == (60, 24)


def test_tiny_terminal_does_not_crash(make_video):
    console = Console(file=io.StringIO(), force_terminal=True, width=20, height=8, color_system=None)
    session, *_ = make_session(make_video(frames=20, fps=20.0), duration=0.5, console=console)
    assert session.run().completed


# ------------------------------------------------------------------ video problems


def test_truncated_video_file_does_not_hang_or_freeze_the_session(make_video, tmp_path):
    good = make_video("good.avi", frames=60, fps=20.0)
    data = good.read_bytes()
    broken = tmp_path / "broken.avi"
    broken.write_bytes(data[: len(data) // 2])  # cut off mid-file
    import cv2

    opened = []

    def counting_factory(path):
        opened.append(path)
        return cv2.VideoCapture(path)

    session, mpv, buf = make_session(broken, duration=2.5,
                                     loader_factory=lambda src: VideoLoader(src, counting_factory))
    t0 = time.monotonic()
    result = session.run()
    assert result.completed and time.monotonic() - t0 < 6  # audio decides when playback ends
    # The file may fail to open at all (audio-only fallback) or run out early; either way the
    # player must not hammer it: at most the first open plus a few back-off reopen attempts.
    assert result.notice or len(opened) <= 1 + 1 + 3
    assert len(opened) <= 5


def test_seek_backwards_moves_the_picture_back(make_video):
    video = make_video(frames=200, fps=20.0)
    script = [(1.0, "\xe0"), (1.0, "K"), (1.8, "q")]  # at 1.0 s press LEFT (seek -5 s -> clock 0)
    session, mpv, _ = make_session(video, duration=30.0, script=script)
    result = session.run()
    assert ("seek", -5.0) in mpv.calls and result.seeks >= 1
    assert session._bg.last_delivered < 25  # without the seek it would be ~36 (1.8 s * 20 fps)


def test_stream_drop_shows_a_reconnecting_notice(make_video):
    video = make_video(frames=60, fps=20.0)
    factory = flaky_factory(fail_after_first=15)
    session, mpv, buf = make_session(video, duration=3.0,
                                     loader_factory=lambda src: VideoLoader(src, factory))
    # network behaviour: keep retrying instead of treating the failure as the end of a local file
    original_open = VideoLoader.open

    def open_as_stream(self):
        self.is_stream = True
        return original_open(self)

    VideoLoader.open = open_as_stream  # type: ignore[method-assign]
    try:
        result = session.run()
    finally:
        VideoLoader.open = original_open  # type: ignore[method-assign]
    out = buf.getvalue()
    assert result.completed and result.video_recoveries >= 1
    assert "reconnecting" in out or "reconnected" in out


# ------------------------------------------------------------------ lyrics


def test_lyrics_service_outage_is_shown_and_playback_continues():
    down = Lyrics(LyricsStatus.ERROR, message="Lyrics service is slow or unreachable (timed out).")
    session, mpv, buf = make_session(duration=1.0, lyrics=down)
    result = session.run()
    out = buf.getvalue()
    assert result.completed and "Lyrics service unavailable" in out and "timed out" in out


def test_slow_lyrics_lookup_never_delays_start_or_quit():
    release = threading.Event()

    def slow():
        release.wait(10)
        return Lyrics(LyricsStatus.NOT_FOUND)

    session, mpv, _ = make_session(duration=5.0, script=[(0.4, "q")])
    from src.lyrics.lyrics_sync import LyricsTrack

    session._lyrics = LyricsTrack(slow)
    t0 = time.monotonic()
    result = session.run()
    elapsed = time.monotonic() - t0
    release.set()
    assert result.quit_by_user and elapsed < 3
    assert_workers_stop(timeout=3)


def test_lyric_lines_are_highlighted_at_the_right_time():
    lyrics = Lyrics(LyricsStatus.SYNCED, (LyricLine(0.0, "FIRST"), LyricLine(0.5, "SECOND"),
                                           LyricLine(1.0, "THIRD")))
    session, mpv, buf = make_session(duration=1.4, lyrics=lyrics)
    assert session.run().completed
    out = buf.getvalue()
    assert out.index("FIRST") < out.index("SECOND") < out.index("THIRD")


def test_make_lyrics_fetcher_uses_the_configured_cache_and_closes_http(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"duration": 200, "syncedLyrics": "[00:01.00]hello"})

    closed = []

    class MockedProvider(LrclibProvider):
        def __init__(self, base_url, timeout):
            super().__init__(base_url, timeout, httpx.Client(transport=httpx.MockTransport(handler)))

        def close(self):
            closed.append(True)

    monkeypatch.setattr(cli, "LrclibProvider", MockedProvider)
    media = ResolvedMedia(MediaMetadata("Song", "Band", 200), MediaKind.AUDIO, "x", None, "Local file")
    cfg = AppConfig(cache_dir=tmp_path / "mycache", lyrics_cache_max_entries=10)
    assert cli.make_lyrics_fetcher(media, cfg)().is_synced
    assert cli.make_lyrics_fetcher(media, cfg)().is_synced  # second time: served from disk
    assert calls == ["/api/get"] and len(closed) == 2
    assert list((tmp_path / "mycache" / "lyrics").glob("*.json"))


def test_lyrics_cache_is_bounded(tmp_path):
    cache = LyricsCache(tmp_path, max_entries=3)
    result = Lyrics(LyricsStatus.SYNCED, (LyricLine(1, "a"),))
    for i in range(6):
        cache.put(LyricsQuery(f"title {i}", "artist"), result)
        time.sleep(0.01)  # distinct modification times
    assert len(list(tmp_path.glob("*.json"))) == 3
    assert cache.get(LyricsQuery("title 5", "artist")) is not None   # newest kept
    assert cache.get(LyricsQuery("title 0", "artist")) is None       # oldest evicted
    off = LyricsCache(tmp_path / "off", max_entries=0)
    off.put(LyricsQuery("t", "a"), result)
    assert off.get(LyricsQuery("t", "a")) is None and not (tmp_path / "off").exists()


# ------------------------------------------------------------------ statistics / logging


def test_performance_statistics_are_collected_and_logged(make_video, tmp_path):
    log_path = configure_logging(True, "INFO", tmp_path / "logs")
    try:
        session, mpv, _ = make_session(make_video(frames=40, fps=20.0), duration=1.2)
        result = session.run()
        for handler in logging.getLogger("ascii_player").handlers:
            handler.flush()
    finally:
        configure_logging(False)
    assert result.redraws > 3 and result.avg_render_ms > 0 and result.avg_convert_ms > 0
    assert result.frames_delivered > 10 and result.cpu_percent >= 0  # Windows CPU accounting is tick-based
    text = log_path.read_text(encoding="utf-8")
    assert "Session start" in text and "Session end (completed)" in text
    assert "redraws" in text and "cpu" in text and "mpv clock gap" in text


def test_playback_error_is_logged_without_urls(make_video, tmp_path):
    log_path = configure_logging(True, "INFO", tmp_path / "logs")
    try:
        session, mpv, _ = make_session(duration=10.0)
        threading.Timer(0.3, lambda: session._audio.clock.set_error(
            "Audio playback failed: https://x.googlevideo.com/videoplayback?sig=SECRET&key=K")).start()
        session.run()
        for handler in logging.getLogger("ascii_player").handlers:
            handler.flush()
    finally:
        configure_logging(False)
    text = log_path.read_text(encoding="utf-8")
    assert "Session error" in text and "SECRET" not in text and "key=K" not in text


# ------------------------------------------------------------------ quality keys


def test_quality_keys_switch_profile_while_playing(make_video):
    from src.core.config import Quality

    video = make_video(frames=200, fps=30.0)
    config = AppConfig(alt_screen=False)  # no custom width/charset: the profile decides everything
    script = [(0.4, "1"), (0.9, "3"), (1.4, "2"), (1.9, "q")]
    session, mpv, buf = make_session(video, duration=30.0, script=script, config=config,
                                     console=Console(file=io.StringIO(), force_terminal=True, width=170,
                                                     height=50, color_system=None))
    seen = []
    original = session._render

    def record(loading=False):
        original(loading)
        if not loading and session._converter is not None:
            seen.append((session._config.quality, session._converter.charset,
                         session._converter.processor.interpolation, session._converter.processor.width))

    session._render = record
    result = session.run()
    assert result.quit_by_user and not result.error
    qualities = []
    for entry in seen:
        if not qualities or qualities[-1][0] is not entry[0]:
            qualities.append(entry)
    assert [q[0] for q in qualities] == [Quality.BALANCED, Quality.LOW, Quality.HIGH, Quality.BALANCED]
    low, high = qualities[1], qualities[2]
    assert low[1] == Quality.LOW.charset and low[2] == "linear" and low[3] <= 80        # coarse, fast, narrow
    assert high[1] == Quality.HIGH.charset and high[2] == "area" and high[3] > 80       # detailed, wider
    out = session._console.file.getvalue()
    for needle in ("Quality: low", "Quality: high", "Quality: medium", "quality high"):
        assert needle in out, needle


def test_quality_switch_updates_the_frame_cadence_cap(make_video):
    from src.core.config import Quality

    session, *_ = make_session(make_video(frames=30, fps=30.0), duration=0.5)
    session._open_video()
    assert session._compute_k_cap() == 1                     # medium: 30 redraws/s for 30 fps video
    session._set_quality(Quality.LOW)
    assert session._k_cap == 2 and session._k == 2            # low: 15 redraws/s -> every 2nd frame
    session._set_quality(Quality.HIGH)
    assert session._k_cap == 1
    session._set_quality(Quality.HIGH)                        # same again: just a notice
    assert session._state.notice == "Quality: high"
    session._loader.close()


def test_custom_settings_survive_a_quality_switch(make_video):
    from src.core.config import Quality

    cfg = AppConfig(alt_screen=False, ascii_width=50, charset=" .#", ui_fps=10)
    session, *_ = make_session(make_video(frames=20, fps=30.0), duration=0.5, config=cfg)
    session._open_video()
    session._set_quality(Quality.HIGH)
    assert session._config.active_charset == " .#" and session._config.effective_width == 50
    assert session._config.effective_ui_fps == 10
    session._loader.close()
