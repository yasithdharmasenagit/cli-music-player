"""A dropped network stream must not freeze the picture for the rest of the song."""

from __future__ import annotations

import io
import itertools
import threading
import time

import cv2
from rich.console import Console

from src.audio.audio_controller import AudioController
from src.audio.playback_clock import PlaybackClock
from src.core.config import AppConfig
from src.media.media_resolver import ResolvedMedia
from src.media.metadata import MediaKind, MediaMetadata
from src.player.session import PlaybackSession
from src.ui.keyboard import KeyReader
from src.video.video_loader import VideoLoader
from src.video.video_sync import BackgroundVideoSource, SyncedVideoSource


class FlakyCapture:
    """Real capture that starts failing every read after *fail_after* successful frames."""

    def __init__(self, path: str, fail_after: int | None):
        self._cap = cv2.VideoCapture(path)
        self._left = fail_after
        self.released = False

    def _alive(self) -> bool:
        if self._left is None:
            return True
        if self._left <= 0:
            return False
        self._left -= 1
        return True

    def isOpened(self):
        return self._cap.isOpened()

    def get(self, prop):
        return self._cap.get(prop)

    def set(self, prop, value):
        return self._cap.set(prop, value) if self._left is None or self._left > 0 else False

    def read(self):
        return self._cap.read() if self._alive() else (False, None)

    def grab(self):
        return self._cap.grab() if self._alive() else False

    def release(self):
        self.released = True
        self._cap.release()


def flaky_factory(fail_after_first: int):
    """First capture dies after N frames; later ones work normally."""
    made: list[FlakyCapture] = []

    def factory(path):
        cap = FlakyCapture(path, fail_after_first if not made else None)
        made.append(cap)
        return cap

    factory.made = made  # type: ignore[attr-defined]
    return factory


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_loader_reopen_replaces_and_releases_capture(make_video):
    path = make_video(frames=20, fps=10.0)
    factory = flaky_factory(fail_after_first=100)
    with VideoLoader(path, factory) as loader:
        assert loader.reopen(8)
        assert factory.made[0].released and len(factory.made) == 2
        ok, frame = loader.read()
        assert ok and frame is not None
    assert factory.made[1].released


def test_loader_reopen_failure_keeps_loader_retryable(make_video):
    path = make_video(frames=20, fps=10.0)
    calls = {"n": 0}

    def factory(p):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("network down")
        return cv2.VideoCapture(p)

    with VideoLoader(path, factory) as loader:
        assert loader.reopen(3) is False and loader.is_open
        assert loader.reopen(3) is True


def test_source_recovers_from_midstream_failure(make_video):
    path = make_video(frames=60, fps=10.0)
    factory = flaky_factory(fail_after_first=5)
    ft = FakeTime()
    with VideoLoader(path, factory) as loader:
        src = SyncedVideoSource(loader, clock=ft)
        shown = [src.frame_at(i / 10)[0] for i in range(5)]  # frames 0..4 decode fine
        assert shown == [0, 1, 2, 3, 4]
        assert src.frame_at(0.5) is None and src.recovering and not src.exhausted  # read died
        ft.t += 1.0
        got = src.frame_at(0.6)  # reopens at the clock's frame, so playback resumes
        assert got is not None and got[0] == 6
        assert src.recoveries == 1 and not src.recovering and not src.exhausted
        assert src.frame_at(0.7)[0] == 7  # and keeps going normally


def test_real_end_of_video_is_not_retried(make_video):
    path = make_video(frames=10, fps=10.0)
    factory = flaky_factory(fail_after_first=100)
    with VideoLoader(path, factory) as loader:
        src = SyncedVideoSource(loader)
        assert src.frame_at(0.9)[0] == 9
        assert src.frame_at(50.0) is None
        assert src.recoveries == 0 and len(factory.made) == 1


def test_recovery_backoff_and_eventual_success(make_video):
    path = make_video(frames=100, fps=10.0)
    ft = FakeTime()
    attempts = []
    with VideoLoader(path) as loader:
        src = SyncedVideoSource(loader, clock=ft)
        src.frame_at(0.0)
        real_reopen = loader.reopen
        outcomes = itertools.chain([False, False, False], itertools.repeat(True))

        def reopen(i):
            attempts.append(round(ft.t, 1))
            return next(outcomes) and real_reopen(i)

        loader.reopen = reopen  # type: ignore[method-assign]
        real_read = loader.read
        loader.read = lambda: (False, None)  # type: ignore[method-assign]
        src.frame_at(0.5)  # a failed read starts recovery
        for _ in range(40):
            ft.t += 0.5
            src.frame_at(0.5)
        loader.read = real_read  # type: ignore[method-assign]
        for _ in range(10):
            ft.t += 5
            if src.frame_at(0.5) is not None:
                break
    gaps = [round(b - a, 1) for a, b in zip(attempts, attempts[1:])]
    assert gaps[:3] == [0.5, 0.5, 1.0]  # delays 0, 0.5, 1, 2, 4 s: back-off, not a reconnect storm
    assert src.recoveries == 1


def test_background_source_never_blocks_the_ui_thread(make_video):
    path = make_video(frames=40, fps=10.0)
    release = threading.Event()
    with VideoLoader(path) as loader:
        real_read = loader.read

        def slow_read():
            release.wait(5)  # a stalled network read
            return real_read()

        src = SyncedVideoSource(loader)
        src.frame_at(0.0)
        loader.read = slow_read  # type: ignore[method-assign]
        bg = BackgroundVideoSource(src, position=lambda: 1.0)
        bg.start()
        t0 = time.monotonic()
        for _ in range(100):
            assert bg.take() is None  # worker is stuck in the read; UI thread is not
        assert time.monotonic() - t0 < 0.5
        release.set()
        deadline = time.time() + 3
        got = None
        while got is None and time.time() < deadline:
            got = bg.take()
            time.sleep(0.01)
        bg.stop()
    assert got is not None and got[0] == 10


def test_background_source_pause_and_stop(make_video):
    path = make_video(frames=40, fps=10.0)
    with VideoLoader(path) as loader:
        src = SyncedVideoSource(loader)
        src.frame_at(0.0)
        paused = {"v": True}
        bg = BackgroundVideoSource(src, position=lambda: 2.0, pause=lambda: paused["v"])
        bg.start()
        time.sleep(0.2)
        assert bg.take() is None  # idle while video is hidden
        paused["v"] = False
        deadline = time.time() + 3
        got = None
        while got is None and time.time() < deadline:
            got = bg.take()
            time.sleep(0.01)
        assert got is not None and got[0] == 20
        bg.stop()
        bg.stop()  # idempotent


class FakeMpv:
    def __init__(self, clock, duration):
        self.clock, self.duration = clock, duration
        self._pos, self._paused, self._stop = 0.0, True, threading.Event()

    def start(self, source, volume, user_agent=None):
        self.clock.update(position=0.0, paused=True, duration=self.duration)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        last = time.monotonic()
        while not self._stop.is_set():
            time.sleep(0.01)
            now = time.monotonic()
            if not self._paused:
                self._pos += now - last
                if self._pos >= self.duration:
                    self.clock.mark_ended()
                    return
                self.clock.update(position=self._pos)
            last = now

    def set_pause(self, paused):
        self._paused = paused
        self.clock.update(paused=paused)

    def seek_relative(self, s): ...
    def seek_absolute(self, s): ...
    def set_volume(self, v): ...

    def stop(self):
        self._stop.set()


def test_session_keeps_picture_moving_after_stream_drop(make_video):
    """End to end: the stream dies at ~1 s; the picture must still follow the clock afterwards."""
    path = make_video(frames=60, fps=20.0)  # 3 s
    factory = flaky_factory(fail_after_first=20)  # connection drops after ~1 s of video
    clock = PlaybackClock()
    mpv = FakeMpv(clock, 3.0)
    console = Console(file=io.StringIO(), force_terminal=True, width=100, height=30)
    media = ResolvedMedia(MediaMetadata("Song", "Band", 3.0), MediaKind.VIDEO, "a", str(path),
                          "YouTube")
    session = PlaybackSession(media, AppConfig(ascii_width=40, alt_screen=False),
                              AudioController(mpv, clock), None, console,
                              KeyReader(None, windows=True),
                              loader_factory=lambda src: VideoLoader(src, factory))
    result = session.run()
    assert result.completed
    assert result.video_recoveries >= 1
    assert session._source.shown_index >= 45  # kept following the clock past the drop
    assert not session._source.exhausted
    assert all(cap.released for cap in factory.made)  # nothing leaked
