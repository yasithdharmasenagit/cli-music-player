"""Even frame cadence: the picture must advance in constant steps (no judder).

A fixed redraw rate that does not divide the video frame rate (24 redraws/s vs 25/30/50/60 fps)
shows frames at uneven intervals, which looks like shaking/jumping. The player therefore shows
every k-th frame, k a whole number.
"""

from __future__ import annotations

import collections

import cv2
import numpy as np
import pytest

from src.core.config import AppConfig, Quality
from tests.test_lifecycle import make_session


@pytest.fixture
def clip(tmp_path):
    def _make(fps: int, seconds: float = 3.0):
        path = tmp_path / f"clip_{fps}.avi"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (160, 90))
        for i in range(int(fps * seconds)):
            img = np.zeros((90, 160, 3), np.uint8)
            cv2.circle(img, (10 + (i * 3) % 140, 45), 10, (255, 255, 255), -1)
            writer.write(img)
        writer.release()
        return path

    return _make


def shown_gaps(session):
    """Run *session*, recording the index of the frame on screen after every redraw."""
    shown = []
    original = session._render

    def record(loading=False):
        if not loading:
            shown.append(session._last_shown)
        original(loading)

    session._render = record
    assert session.run().completed
    indices: list[int] = []
    for value in shown:  # a redraw for another reason (clock tick) shows the same frame again
        if value >= 0 and (not indices or value != indices[-1]):
            indices.append(value)
    return collections.Counter(b - a for a, b in zip(indices, indices[1:]))


@pytest.mark.parametrize("fps,quality,expected_step", [
    (24, Quality.BALANCED, 1),
    (25, Quality.BALANCED, 1),
    (30, Quality.BALANCED, 1),
    (50, Quality.BALANCED, 2),   # 25 redraws/s
    (60, Quality.BALANCED, 2),   # 30 redraws/s
    (30, Quality.LOW, 2),        # low caps at 15 redraws/s
])
def test_frames_are_shown_at_an_even_cadence(clip, fps, quality, expected_step):
    path = clip(fps)
    config = AppConfig(quality=quality, ascii_width=40, alt_screen=False)
    problems = []
    for attempt in range(2):  # one retry: a stray scheduling hiccup on a busy PC is not a cadence bug
        session, *_ = make_session(path, duration=2.8, config=config)
        gaps = shown_gaps(session)
        total = sum(gaps.values())
        step, count = gaps.most_common(1)[0]
        if total > 20 and step == expected_step and count / total >= 0.95                 and max(gaps) <= 2 * expected_step:
            return
        problems.append(dict(gaps))
    # the old fixed-24-redraws/s behaviour scored 60-89 % identical steps on every attempt
    pytest.fail(f"uneven cadence on both attempts: {problems}")


class FakeSource:
    fps = 30.0


def accepting_session():
    from src.player.session import PlaybackSession

    session, *_ = make_session(None, duration=1.0)
    session._source = FakeSource()  # only .fps is needed by the frame-selection rule
    assert isinstance(session, PlaybackSession)
    return session


def test_k1_accepts_every_frame():
    s = accepting_session()
    s._k = 1
    assert [s._accept_frame(i, 1) for i in range(5)] == [True] * 5


def test_k2_accepts_every_second_frame():
    s = accepting_session()
    decisions = [s._accept_frame(i, 2) for i in range(7)]
    assert decisions == [True, False, True, False, True, False, True]


def test_late_delivery_is_accepted_and_the_cadence_continues_from_there():
    s = accepting_session()
    assert s._accept_frame(0, 2)
    assert s._accept_frame(3, 2)         # the UI was late: frame 2 was already replaced by 3
    assert not s._accept_frame(4, 2)     # next due slot is 5
    assert s._accept_frame(5, 2)


def test_seek_backwards_is_accepted_immediately():
    s = accepting_session()
    for i in (0, 2, 4, 6):
        s._accept_frame(i, 2)
    assert s._accept_frame(1, 2)         # the clock jumped back: show that picture right away
    assert not s._accept_frame(2, 2) and s._accept_frame(3, 2)


def test_slow_terminal_raises_k_by_whole_numbers_then_relaxes():
    s = accepting_session()
    s._k = 1
    s._render_ema = 0.100                # 100 ms per redraw: 3x = 300 ms = 9 frames at 30 fps
    s._accept_frame(0, 1)
    assert s._k == 9
    s._render_ema = 0.001                # the terminal recovers
    s._accept_frame(100, 1)
    assert s._k == 9                     # not immediately: avoid flapping between cadences
    s._k_changed -= 10.0                 # ...only after it has kept up for a few seconds
    s._accept_frame(200, 1)
    assert s._k == 8
