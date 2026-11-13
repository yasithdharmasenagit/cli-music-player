from __future__ import annotations

import numpy as np
import pytest

from src.core.exceptions import InvalidFrameError
from src.video.frame_processor import FrameProcessor


def test_target_size_accounts_for_char_aspect():
    cols, rows = FrameProcessor(width=100, char_aspect=0.5).target_size(1920, 1080)
    assert cols == 100
    assert rows == round(1080 / 1920 * 100 * 0.5)  # 28


def test_max_rows_shrinks_width_keeping_aspect():
    fp = FrameProcessor(width=200, char_aspect=0.5, max_rows=20)
    cols, rows = fp.target_size(100, 100)
    assert rows == 20
    assert cols == 40  # square image, cells 2:1


def test_process_bgr_returns_gray_grid():
    frame = np.full((90, 160, 3), 255, np.uint8)
    out = FrameProcessor(width=40).process(frame)
    assert out.gray.shape == (out.rows, 40)
    assert out.gray.dtype == np.uint8
    assert (out.gray == 255).all()
    assert out.color is None


def test_keep_color_returns_bgr():
    frame = np.zeros((20, 20, 3), np.uint8)
    frame[:, :] = (10, 20, 30)
    out = FrameProcessor(width=10, keep_color=True).process(frame)
    assert out.color is not None and out.color.shape[2] == 3
    assert tuple(out.color[0, 0]) == (10, 20, 30)


def test_bgra_and_single_channel_inputs():
    assert FrameProcessor(width=10).process(np.zeros((20, 20, 4), np.uint8)).cols == 10
    assert FrameProcessor(width=10).process(np.zeros((20, 20, 1), np.uint8)).cols == 10


def test_non_uint8_is_clipped():
    frame = np.full((20, 20), 300.0, np.float32)
    assert (FrameProcessor(width=10).process(frame).gray == 255).all()


@pytest.mark.parametrize("bad", [None, "frame", np.empty((0, 0), np.uint8),
                                 np.zeros((5, 5, 2), np.uint8), np.zeros(5, np.uint8),
                                 np.zeros((5, 5, 3, 1), np.uint8), np.array([["a"]])])
def test_invalid_frames_raise(bad):
    with pytest.raises(InvalidFrameError):
        FrameProcessor(width=10).process(bad)


def test_bad_constructor_args():
    with pytest.raises(ValueError):
        FrameProcessor(width=0)
    with pytest.raises(ValueError):
        FrameProcessor(width=10, char_aspect=0)
