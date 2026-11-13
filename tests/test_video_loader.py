from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest

from src.core.config import AppConfig
from src.core.exceptions import InvalidMediaPathError, VideoLoadError
from src.utils.paths import clean_user_path, validate_media_path
from src.video.ascii_converter import ASCIIConverter
from src.video.video_loader import VideoLoader
from src.video.video_player import PlaybackStatus, VideoPlayer


def test_open_reads_metadata(make_video):
    path = make_video(frames=12, size=(64, 48), fps=10.0)
    with VideoLoader(path) as loader:
        info = loader.info
        assert (info.width, info.height) == (64, 48)
        assert info.fps == pytest.approx(10.0)
        assert info.frame_count == 12
        assert info.duration == pytest.approx(1.2)


def test_reads_all_frames_then_eof(make_video):
    path = make_video(frames=7)
    with VideoLoader(path) as loader:
        count = 0
        while True:
            ok, frame = loader.read()
            if not ok:
                assert frame is None
                break
            assert frame.shape == (48, 64, 3)
            count += 1
        assert count == 7


def test_skip_advances(make_video):
    with VideoLoader(make_video(frames=10)) as loader:
        assert loader.skip(3) == 3
        count = 0
        while loader.read()[0]:
            count += 1
        assert count == 7
        assert loader.skip(5) == 0


def test_missing_file(tmp_path):
    with pytest.raises(VideoLoadError, match="not found"):
        VideoLoader(tmp_path / "nope.mp4").open()


def test_empty_file(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(VideoLoadError, match="empty"):
        VideoLoader(empty).open()


def test_garbage_file(tmp_path):
    bad = tmp_path / "garbage.mp4"
    bad.write_bytes(b"this is not a video" * 100)
    with pytest.raises(VideoLoadError):
        VideoLoader(bad).open()


def test_video_without_frames(make_video):
    path = make_video(frames=0)
    with pytest.raises(VideoLoadError):
        VideoLoader(path).open()


def test_read_before_open_raises(tmp_path):
    with pytest.raises(VideoLoadError):
        VideoLoader(tmp_path / "x.mp4").read()


class SpyCapture:
    def __init__(self, ok: bool = True, frames: int = 2):
        self.ok, self.frames, self.released = ok, frames, 0

    def isOpened(self):
        return self.ok

    def get(self, prop):
        import cv2
        return {cv2.CAP_PROP_FRAME_WIDTH: 8, cv2.CAP_PROP_FRAME_HEIGHT: 8,
                cv2.CAP_PROP_FPS: 0.0, cv2.CAP_PROP_FRAME_COUNT: self.frames}.get(prop, 0)

    def read(self):
        if self.frames <= 0:
            return False, None
        self.frames -= 1
        return True, np.zeros((8, 8, 3), np.uint8)

    def grab(self):
        return self.read()[0]

    def release(self):
        self.released += 1


@pytest.fixture
def fake_file(tmp_path):
    p = tmp_path / "fake.mp4"
    p.write_bytes(b"x")
    return p


def test_cleanup_on_context_exit(fake_file):
    cap = SpyCapture()
    with VideoLoader(fake_file, lambda _p: cap) as loader:
        assert loader.is_open
    assert cap.released == 1
    assert not loader.is_open


def test_cleanup_on_exception_inside_context(fake_file):
    cap = SpyCapture()
    with pytest.raises(RuntimeError):
        with VideoLoader(fake_file, lambda _p: cap):
            raise RuntimeError("boom")
    assert cap.released == 1


def test_cleanup_when_open_fails(fake_file):
    cap = SpyCapture(ok=False)
    with pytest.raises(VideoLoadError):
        VideoLoader(fake_file, lambda _p: cap).open()
    assert cap.released == 1


def test_cleanup_when_no_frames(fake_file):
    cap = SpyCapture(frames=0)
    with pytest.raises(VideoLoadError):
        VideoLoader(fake_file, lambda _p: cap).open()
    assert cap.released == 1


def test_close_is_idempotent_and_default_fps(fake_file):
    cap = SpyCapture()
    loader = VideoLoader(fake_file, lambda _p: cap)
    assert loader.open().fps == 30.0  # invalid reported FPS falls back
    loader.close()
    loader.close()
    assert cap.released == 1
    with pytest.raises(VideoLoadError):
        loader.read()


def test_validate_media_path(tmp_path):
    f = tmp_path / "a b.mp4"
    f.write_bytes(b"x")
    assert validate_media_path(f'"{f}"') == f.resolve()
    assert validate_media_path(f"& '{f}'") == f.resolve()
    for bad in ["", "   ", str(tmp_path / "missing.mp4")]:
        with pytest.raises(InvalidMediaPathError):
            validate_media_path(bad)
    with pytest.raises(InvalidMediaPathError, match="Not a file"):
        validate_media_path(tmp_path)
    assert clean_user_path("  'x'  ") == "x"


class ListSink:
    def __init__(self):
        self.started = self.stopped = 0
        self.frames: list[str] = []
        self.statuses: list[PlaybackStatus] = []

    def start(self):
        self.started += 1

    def draw(self, frame, status):
        self.frames.append(frame)
        self.statuses.append(status)

    def stop(self):
        self.stopped += 1


def test_player_plays_to_eof_and_stops_sink(make_video):
    sink = ListSink()
    cfg = AppConfig(ascii_width=20, fps=1000.0, max_fps=1000.0, drop_frames=False)
    with VideoLoader(make_video(frames=8)) as loader:
        stats = VideoPlayer(loader, ASCIIConverter(width=20), sink, cfg).play()
    assert stats.completed and not stats.interrupted
    assert stats.frames_shown == 8 == len(sink.frames)
    assert sink.started == sink.stopped == 1
    assert [s.frame_index for s in sink.statuses] == list(range(1, 9))


def test_player_respects_fps_timing(make_video):
    sink = ListSink()
    cfg = AppConfig(ascii_width=20, drop_frames=False)  # native 10 fps, 10 frames
    with VideoLoader(make_video(frames=10, fps=10.0)) as loader:
        stats = VideoPlayer(loader, ASCIIConverter(width=20), sink, cfg).play()
    assert 0.85 <= stats.elapsed <= 1.5  # ~1 s


def test_player_drops_frames_when_slow(make_video):
    class SlowSink(ListSink):
        def draw(self, frame, status):
            import time
            time.sleep(0.15)
            super().draw(frame, status)

    sink = SlowSink()
    cfg = AppConfig(ascii_width=20)
    with VideoLoader(make_video(frames=10, fps=10.0)) as loader:
        stats = VideoPlayer(loader, ASCIIConverter(width=20), sink, cfg).play()
    assert stats.frames_dropped > 0
    assert stats.frames_shown + stats.frames_dropped <= 10


def test_player_stops_sink_on_keyboard_interrupt(make_video):
    class Interrupting(ListSink):
        def draw(self, frame, status):
            super().draw(frame, status)
            if len(self.frames) == 2:
                raise KeyboardInterrupt

    sink = Interrupting()
    cfg = AppConfig(ascii_width=20, fps=1000.0, max_fps=1000.0)
    with VideoLoader(make_video(frames=8)) as loader:
        stats = VideoPlayer(loader, ASCIIConverter(width=20), sink, cfg).play()
    assert stats.interrupted and not stats.completed
    assert sink.stopped == 1


def test_player_stop_event(make_video):
    sink = ListSink()
    event = threading.Event()
    event.set()
    with VideoLoader(make_video(frames=5)) as loader:
        stats = VideoPlayer(loader, ASCIIConverter(width=20), sink,
                            AppConfig(ascii_width=20)).play(event)
    assert stats.frames_shown == 0 and sink.stopped == 1
