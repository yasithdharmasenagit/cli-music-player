"""Picture steadiness: shimmer suppression and steady frame timing."""

from __future__ import annotations

import sys

import numpy as np
import pytest

from src.cli import main as cli
from src.core.config import CHARSET_BALANCED, CHARSET_HIGH, CHARSET_STANDARD, DEFAULT_CONFIG
from src.core.user_config import parse_config_text
from src.utils.timer import high_resolution_timer
from src.video.ascii_converter import ASCIIConverter
from tests.test_lifecycle import make_session


def frame(value: int, size=(90, 160)) -> np.ndarray:
    return np.full((*size, 3), value, np.uint8)


def noisy(base: int, amplitude: int, seed: int, size=(90, 160)) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.integers(-amplitude, amplitude + 1, size, dtype=np.int16)
    return np.clip(base + noise[:, :, None], 0, 255).astype(np.uint8).repeat(1, axis=2)[:, :, :1].repeat(3, axis=2)


def changed_cells(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b))


# ------------------------------------------------------------------ thresholds


def test_threshold_follows_the_ramp_and_has_a_floor():
    assert ASCIIConverter(charset=CHARSET_STANDARD).stability_threshold == 26   # 10 levels
    assert ASCIIConverter(charset=CHARSET_BALANCED).stability_threshold == 11   # 24 levels
    assert ASCIIConverter(charset=CHARSET_HIGH).stability_threshold == 8        # 70 levels (floor)


# ------------------------------------------------------------------ behaviour


@pytest.mark.parametrize("charset", [CHARSET_STANDARD, CHARSET_BALANCED, CHARSET_HIGH])
def test_noise_below_the_threshold_does_not_change_any_character(charset):
    conv = ASCIIConverter(width=60, charset=charset, stabilize=True)
    base = 120
    first = conv.convert(noisy(base, 0, 0))
    amp = conv.stability_threshold // 2 - 1          # compression-noise sized wobble
    for seed in range(1, 12):
        assert conv.convert(noisy(base, amp, seed)) == first


def test_without_stabilization_the_same_noise_flickers():
    conv = ASCIIConverter(width=60, charset=CHARSET_HIGH, stabilize=False)
    first = conv.convert(noisy(120, 0, 0))
    flickers = sum(changed_cells(first, conv.convert(noisy(120, 3, s))) for s in range(1, 6))
    assert flickers > 50  # the problem being solved


def test_real_changes_still_show():
    conv = ASCIIConverter(width=40, charset=CHARSET_HIGH, stabilize=True)
    dark, bright = conv.convert(frame(40)), None
    bright = conv.convert(frame(200))
    assert dark != bright and set(bright) != set(dark)
    assert conv.convert(frame(40)) == dark  # and back again


def test_slow_fades_are_not_frozen():
    """Brightness creeping up a little each frame must eventually update the cell."""
    conv = ASCIIConverter(width=40, charset=CHARSET_BALANCED, stabilize=True)
    outputs = {conv.convert(frame(v)) for v in range(60, 200, 2)}  # +2 levels per frame
    assert len(outputs) > 5


def test_a_seek_or_scene_cut_updates_every_cell_at_once():
    conv = ASCIIConverter(width=40, charset=CHARSET_HIGH, stabilize=True)
    conv.convert(frame(30))
    assert conv.convert(frame(220)) == ASCIIConverter(width=40, charset=CHARSET_HIGH).convert(frame(220))


def test_stabilized_output_stays_close_to_the_true_image():
    rng = np.random.default_rng(3)
    img = rng.integers(0, 256, (90, 160), dtype=np.uint8)
    plain = ASCIIConverter(width=60, charset=CHARSET_HIGH)
    calm = ASCIIConverter(width=60, charset=CHARSET_HIGH, stabilize=True)
    calm.convert(img)  # first frame is shown exactly
    again = calm.convert(img)
    assert again == plain.convert(img)  # identical input: identical picture


def test_stabilizer_resets_when_the_grid_changes_size():
    conv = ASCIIConverter(width=40, stabilize=True)
    a = conv.convert(frame(100, (90, 160)))
    b = conv.convert(frame(100, (160, 160)))   # different aspect: new grid, no stale state
    assert a.count("\n") != b.count("\n")


def test_color_mode_is_stabilized_with_the_same_mask():
    conv = ASCIIConverter(width=40, charset=CHARSET_HIGH, color=True, stabilize=True)
    base = np.zeros((90, 160, 3), np.uint8)
    base[:] = (60, 120, 180)
    first = conv.convert(base)
    wobble = base.copy()
    wobble[:] = (63, 122, 183)                     # tiny change: same characters and colours
    assert conv.convert(wobble) == first
    assert conv.convert(np.full((90, 160, 3), 240, np.uint8)) != first


def test_stabilization_adds_no_per_frame_allocation_growth():
    import tracemalloc

    conv = ASCIIConverter(width=100, charset=CHARSET_HIGH, stabilize=True)
    img = noisy(100, 3, 1, (360, 640))
    for _ in range(10):
        conv.convert(img)
    tracemalloc.start()
    start = tracemalloc.get_traced_memory()[0]
    for _ in range(200):
        conv.convert(img)
    growth = tracemalloc.get_traced_memory()[0] - start
    tracemalloc.stop()
    assert growth < 8 * 1024


# ------------------------------------------------------------------ settings


def test_stabilize_is_on_by_default_and_configurable():
    assert DEFAULT_CONFIG.stabilize is True
    assert parse_config_text("[video]\nstabilize = false\n")[0].stabilize is False
    with pytest.raises(Exception, match="stabilize"):
        parse_config_text('[video]\nstabilize = "no"\n')
    parser = cli.build_parser()
    assert cli.config_from_args(parser.parse_args(["--no-stabilize"])).stabilize is False
    assert cli.config_from_args(parser.parse_args([])).stabilize is True


def test_session_passes_the_setting_to_the_converter(make_video):
    from src.core.config import AppConfig

    for flag in (True, False):
        session, *_ = make_session(make_video(frames=20, fps=20.0), duration=0.4,
                                   config=AppConfig(ascii_width=40, alt_screen=False, stabilize=flag))
        assert session.run().completed
        assert session._converter.stabilize is flag


# ------------------------------------------------------------------ timer resolution


def test_timer_resolution_is_requested_and_restored(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows only")
    import ctypes

    calls = []

    class FakeWinmm:
        def timeBeginPeriod(self, ms):
            calls.append(("begin", ms))
            return 0

        def timeEndPeriod(self, ms):
            calls.append(("end", ms))
            return 0

    monkeypatch.setattr(ctypes, "WinDLL", lambda name: FakeWinmm())
    with high_resolution_timer(1) as active:
        assert active is True and calls == [("begin", 1)]
    assert calls == [("begin", 1), ("end", 1)]  # always balanced, so the system setting is undone


def test_timer_is_restored_even_if_playback_raises(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows only")
    import ctypes

    calls = []

    class FakeWinmm:
        def timeBeginPeriod(self, ms):
            calls.append("begin")
            return 0

        def timeEndPeriod(self, ms):
            calls.append("end")
            return 0

    monkeypatch.setattr(ctypes, "WinDLL", lambda name: FakeWinmm())
    with pytest.raises(RuntimeError):
        with high_resolution_timer():
            raise RuntimeError("boom")
    assert calls == ["begin", "end"]


def test_timer_helper_is_harmless_elsewhere_or_on_failure(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with high_resolution_timer() as active:
        assert active is False
    monkeypatch.setattr(sys, "platform", "win32")
    import ctypes

    def broken(name):
        raise OSError("no winmm")

    monkeypatch.setattr(ctypes, "WinDLL", broken)
    with high_resolution_timer() as active:
        assert active is False  # failure never breaks playback


def test_session_holds_the_fine_timer_only_while_playing(monkeypatch, make_video):
    import contextlib

    import src.player.session as session_module

    events = []

    @contextlib.contextmanager
    def recording():
        events.append("enter")
        try:
            yield True
        finally:
            events.append("exit")

    monkeypatch.setattr(session_module, "high_resolution_timer", recording)
    session, *_ = make_session(make_video(frames=10, fps=20.0), duration=0.3)
    assert session.run().completed
    assert events == ["enter", "exit"]
