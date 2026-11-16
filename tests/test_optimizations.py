"""Performance-related behaviour: buffer reuse, bounded memory, decode-ahead correctness."""

from __future__ import annotations

import threading
import time
import tracemalloc

import numpy as np
import pytest

from src.audio.playback_clock import PlaybackClock
from src.core.config import CHARSET_HIGH, CHARSET_STANDARD
from src.video.ascii_converter import ASCIIConverter
from src.video.frame_processor import FrameProcessor
from src.video.video_loader import VideoLoader
from src.video.video_sync import BackgroundVideoSource, SyncedVideoSource, target_frame_index


def noise(h=360, w=640, channels=3, seed=1):
    rng = np.random.default_rng(seed)
    shape = (h, w, channels) if channels else (h, w)
    return rng.integers(0, 256, shape, dtype=np.uint8)


# ------------------------------------------------------------------ converter / processor


def test_byte_path_matches_unicode_path_exactly():
    frame = noise()
    fast = ASCIIConverter(width=80, charset=CHARSET_HIGH)
    slow = ASCIIConverter(width=80, charset=CHARSET_HIGH)
    slow._byte_table = None  # force the generic Unicode path
    assert fast.convert(frame) == slow.convert(frame)
    inv_fast = ASCIIConverter(width=80, charset=CHARSET_STANDARD, invert=True)
    inv_slow = ASCIIConverter(width=80, charset=CHARSET_STANDARD, invert=True)
    inv_slow._byte_table = None
    assert inv_fast.convert(frame) == inv_slow.convert(frame)


def test_unicode_charsets_still_work_without_byte_table():
    conv = ASCIIConverter(width=20, charset=" ░▒▓█")
    assert conv._byte_table is None
    assert set(conv.convert(np.full((40, 40), 255, np.uint8))) == {"█", "\n"}


def test_converter_reuses_its_buffers_between_frames():
    conv = ASCIIConverter(width=60)
    conv.convert(noise(seed=1))
    out, mapped = conv._out, conv._mapped
    small, gray = conv.processor._small, conv.processor._gray
    for seed in (2, 3, 4):
        conv.convert(noise(seed=seed))
    assert conv._out is out and conv._mapped is mapped  # no per-frame allocation
    assert conv.processor._small is small and conv.processor._gray is gray
    conv.convert(noise(h=100, w=640))  # grid changes: buffers are replaced exactly once
    assert conv._out is not out and conv._out.shape[0] != out.shape[0]


def test_results_do_not_leak_between_frames_when_buffers_are_reused():
    conv = ASCIIConverter(width=40, charset=" .:#")
    black = conv.convert(np.zeros((80, 80, 3), np.uint8))
    white = conv.convert(np.full((80, 80, 3), 255, np.uint8))
    assert set(black) == {" ", "\n"} and set(white) == {"#", "\n"}
    assert black != white and isinstance(black, str)  # strings are independent copies


def test_public_processor_returns_independent_arrays_by_default():
    fp = FrameProcessor(width=20)
    a = fp.process(noise(seed=1))
    b = fp.process(noise(seed=2))
    assert not np.shares_memory(a.gray, b.gray)
    reuse = FrameProcessor(width=20, reuse_buffers=True)
    assert np.shares_memory(reuse.process(noise(seed=1)).gray, reuse.process(noise(seed=2)).gray)


def test_bgra_and_gray_work_with_reused_buffers():
    conv = ASCIIConverter(width=30)
    assert len(conv.convert(noise(channels=4)).split("\n")[0]) == 30
    assert len(conv.convert(noise(channels=0)).split("\n")[0]) == 30
    assert len(conv.convert(noise(channels=3)).split("\n")[0]) == 30


@pytest.mark.parametrize("interp", ["area", "linear"])
def test_both_interpolations_give_same_geometry(interp):
    conv = ASCIIConverter(width=50, interpolation=interp)
    lines = conv.convert(noise(h=720, w=1280)).split("\n")
    assert len(lines[0]) == 50 and len(lines) == round(720 / 1280 * 50 * 0.5)


def test_invalid_interpolation_rejected():
    with pytest.raises(ValueError, match="interpolation"):
        FrameProcessor(width=20, interpolation="cubic")


def test_linear_resize_is_cheaper_than_area():
    frame = noise(h=720, w=1280)
    times = {}
    for interp in ("area", "linear"):
        conv = ASCIIConverter(width=120, interpolation=interp)
        for _ in range(5):
            conv.convert(frame)
        best = float("inf")
        for _ in range(5):  # best-of-5 keeps this robust on a busy machine
            t0 = time.perf_counter()
            for _ in range(20):
                conv.convert(frame)
            best = min(best, time.perf_counter() - t0)
        times[interp] = best
    assert times["linear"] < times["area"]  # measured ~100x on the dev machine; only ordering is asserted


def test_conversion_allocates_little():
    """Per-conversion Python allocations stay far below the old ~72 KiB (measured ~4.6 KiB)."""
    conv = ASCIIConverter(width=120, charset=CHARSET_HIGH, max_rows=50)
    frame = noise(h=720, w=1280)
    for _ in range(5):
        conv.convert(frame)
    tracemalloc.start()
    tracemalloc.reset_peak()
    base = tracemalloc.get_traced_memory()[0]
    for _ in range(50):
        conv.convert(frame)
    peak = tracemalloc.get_traced_memory()[1] - base
    tracemalloc.stop()
    assert peak < 20 * 1024, f"peak {peak / 1024:.1f} KiB"


def test_no_memory_growth_over_many_conversions():
    conv = ASCIIConverter(width=100)
    frame = noise()
    for _ in range(10):
        conv.convert(frame)
    tracemalloc.start()
    start = tracemalloc.get_traced_memory()[0]
    for _ in range(300):
        conv.convert(frame)
    growth = tracemalloc.get_traced_memory()[0] - start
    tracemalloc.stop()
    assert growth < 8 * 1024


# ------------------------------------------------------------------ decode-ahead buffer


class ManualClock:
    """Position under the test's control."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def wait_until(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


def take_until(bg, cond, timeout=3.0):
    """Poll ``bg.take()`` until a frame satisfying *cond* arrives; returns it (or None)."""
    end = time.time() + timeout
    while time.time() < end:
        got = bg.take()
        if got is not None and cond(got):
            return got
        time.sleep(0.005)
    return None


def test_buffer_is_bounded_and_never_delivers_early(make_video):
    clock = ManualClock()
    with VideoLoader(make_video(frames=60, fps=10.0)) as loader:
        src = SyncedVideoSource(loader)
        src.frame_at(0.0)
        bg = BackgroundVideoSource(src, clock, buffer_frames=5, low_watermark=1)
        bg.start()
        assert wait_until(lambda: bg.buffered == 5)  # fills to capacity, then stops decoding
        time.sleep(0.2)
        assert bg.buffered == 5 and src.next_index == 6  # decoded 1..5 and no further
        assert bg.take() is None  # clock is at 0.0: frames 1..5 are in the future
        clock.t = 0.25  # frame 2 is now due; 1 is older
        got = bg.take()
        assert got is not None and got[0] == 2
        assert bg.take() is None
        clock.t = 0.35
        assert bg.take()[0] == 3
        bg.stop()
        assert bg.buffered == 0 and bg.delivered == 2 and bg.last_delivered == 3


def test_buffer_refills_after_frames_are_consumed(make_video):
    clock = ManualClock()
    with VideoLoader(make_video(frames=60, fps=10.0)) as loader:
        src = SyncedVideoSource(loader)
        src.frame_at(0.0)
        bg = BackgroundVideoSource(src, clock, buffer_frames=4, low_watermark=1)
        bg.start()
        delivered = []
        for step in range(1, 30):
            clock.t = step / 10.0 + 0.01
            assert wait_until(lambda: bg.buffered >= 1 or src.exhausted)
            got = bg.take()
            if got:
                delivered.append(got[0])
            assert bg.buffered <= bg.capacity
            time.sleep(0.01)
        bg.stop()
    assert delivered == list(range(1, len(delivered) + 1))  # every frame, in order, no gaps


def test_buffer_capacity_is_limited_by_bytes(make_video):
    with VideoLoader(make_video(frames=10, fps=10.0, size=(64, 48))) as loader:
        src = SyncedVideoSource(loader)
        frame_bytes = src.frame_bytes
        assert frame_bytes == 64 * 48 * 3
        assert BackgroundVideoSource(src, lambda: 0.0, buffer_frames=8,
                                     max_buffer_bytes=frame_bytes * 3).capacity == 3
        assert BackgroundVideoSource(src, lambda: 0.0, buffer_frames=8,
                                     max_buffer_bytes=frame_bytes // 2).capacity == 1  # huge frames
        assert BackgroundVideoSource(src, lambda: 0.0, buffer_frames=1).capacity == 1
        big = BackgroundVideoSource(src, lambda: 0.0)
        assert big.capacity <= 8 and big.low_watermark < big.capacity


def test_backward_jump_discards_stale_lookahead(make_video):
    clock = ManualClock()
    with VideoLoader(make_video(frames=100, fps=10.0)) as loader:
        src = SyncedVideoSource(loader)
        src.frame_at(0.0)
        bg = BackgroundVideoSource(src, clock, buffer_frames=6, low_watermark=2)
        bg.start()
        clock.t = 6.0
        assert take_until(bg, lambda g: g[0] == 60) is not None
        clock.t = 1.0  # user seeks back 5 s
        got = take_until(bg, lambda g: g[0] <= 11)
        assert got is not None and got[0] in (10, 11)
        assert src.seeks >= 1
        bg.stop()


def test_forward_jump_catches_up_via_seek(make_video):
    clock = ManualClock()
    with VideoLoader(make_video(frames=200, fps=10.0)) as loader:
        src = SyncedVideoSource(loader)
        src.frame_at(0.0)
        bg = BackgroundVideoSource(src, clock, buffer_frames=4, low_watermark=1)
        bg.start()
        clock.t = 15.0
        got = take_until(bg, lambda g: g[0] >= 150)
        assert got is not None and got[0] == 150 and src.seeks >= 1
        bg.stop()


def test_wait_for_frame_wakes_for_due_frame_and_times_out(make_video):
    clock = ManualClock()
    with VideoLoader(make_video(frames=30, fps=10.0)) as loader:
        src = SyncedVideoSource(loader)
        src.frame_at(0.0)
        bg = BackgroundVideoSource(src, clock, buffer_frames=4)
        t0 = time.monotonic()
        assert bg.wait_for_frame(0.05) is False  # nothing decoded yet: plain timeout
        assert 0.04 <= time.monotonic() - t0 < 0.5
        assert bg.seconds_until_next() is None
        bg.start()
        assert wait_until(lambda: bg.seconds_until_next() is not None)
        assert 0.0 < bg.seconds_until_next() <= 0.1 + 1e-6  # frame 1 due at t=0.1
        bg.stop()


def test_stop_joins_worker_and_releases_frames(make_video):
    with VideoLoader(make_video(frames=30, fps=10.0)) as loader:
        src = SyncedVideoSource(loader)
        src.frame_at(0.0)
        bg = BackgroundVideoSource(src, lambda: 0.0)
        bg.start()
        assert wait_until(lambda: bg.buffered > 0)
        bg.stop()
        assert not [t for t in threading.enumerate() if t.name == "video-decode" and t.is_alive()]
        assert bg.buffered == 0 and bg.take() is None


def test_pause_clears_buffer_and_resumes_cleanly(make_video):
    clock = ManualClock()
    hidden = {"v": False}
    with VideoLoader(make_video(frames=100, fps=10.0)) as loader:
        src = SyncedVideoSource(loader)
        src.frame_at(0.0)
        bg = BackgroundVideoSource(src, clock, pause=lambda: hidden["v"], buffer_frames=4)
        bg.start()
        assert wait_until(lambda: bg.buffered > 0)
        hidden["v"] = True  # video hidden with V
        assert wait_until(lambda: bg.buffered == 0)
        clock.t = 5.0  # clock moves on while hidden
        hidden["v"] = False
        got = take_until(bg, lambda g: g[0] >= 50)
        assert got is not None and got[0] == 50
        bg.stop()


def test_target_index_matches_delivery_rule(make_video):
    # delivery uses exactly the same clock->frame rule as the rest of the player
    assert target_frame_index(0.25, 10.0) == 2 and target_frame_index(0.35, 10.0, 100) == 3


def test_clock_object_works_as_position_source():
    clock = PlaybackClock()
    clock.update(position=1.0, paused=True)
    assert clock.position() == 1.0
