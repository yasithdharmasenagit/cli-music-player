from __future__ import annotations

import re

import numpy as np
import pytest

from src.core.config import CHARSET_HIGH, CHARSET_STANDARD
from src.core.exceptions import InvalidFrameError
from src.video.ascii_converter import ASCIIConverter

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def lines(text: str) -> list[str]:
    return text.split("\n")


def test_grayscale_conversion_extremes():
    conv = ASCIIConverter(width=20, charset=" .#")
    black = conv.convert(np.zeros((40, 40), np.uint8))
    white = conv.convert(np.full((40, 40), 255, np.uint8))
    assert set(black) == {" ", "\n"}
    assert set(white) == {"#", "\n"}


def test_bgr_conversion_uses_luminance():
    conv = ASCIIConverter(width=10, charset=" .:-=+*#%@")
    green = np.zeros((20, 20, 3), np.uint8)
    green[:, :, 1] = 255
    blue = np.zeros((20, 20, 3), np.uint8)
    blue[:, :, 0] = 255
    pos = " .:-=+*#%@".index
    # green is far brighter than blue in luminance
    assert pos(conv.convert(green)[0]) > pos(conv.convert(blue)[0])


def test_bgr_and_gray_agree():
    conv = ASCIIConverter(width=16)
    gray = np.tile(np.arange(0, 256, 4, dtype=np.uint8), (64, 1))
    bgr = np.dstack([gray, gray, gray])
    assert conv.convert(gray) == conv.convert(bgr)


@pytest.mark.parametrize("width", [10, 80, 120, 200])
def test_output_width(width):
    out = ASCIIConverter(width=width).convert(np.zeros((90, 160, 3), np.uint8))
    assert {len(l) for l in lines(out)} == {width}


def test_aspect_ratio_preserved():
    # 16:9 source, cells twice as tall as wide -> rows ~ width * 9/16 * 0.5
    out = ASCIIConverter(width=160).convert(np.zeros((90, 160, 3), np.uint8))
    assert len(lines(out)) == 45
    # a square source gets half as many rows as columns
    sq = ASCIIConverter(width=80).convert(np.zeros((100, 100), np.uint8))
    assert len(lines(sq)) == 40


def test_output_size_matches_convert():
    conv = ASCIIConverter(width=70)
    cols, rows = conv.output_size(300, 200)
    out = lines(conv.convert(np.zeros((200, 300, 3), np.uint8)))
    assert (len(out[0]), len(out)) == (cols, rows)


def test_max_rows_limits_height():
    out = ASCIIConverter(width=200, max_rows=10).convert(np.zeros((100, 100), np.uint8))
    assert len(lines(out)) == 10


def test_gradient_is_monotonic_dark_to_bright():
    conv = ASCIIConverter(width=64, charset=CHARSET_HIGH)
    gradient = np.tile(np.linspace(0, 255, 256).astype(np.uint8), (32, 1))
    row = lines(conv.convert(gradient))[0]
    idx = [CHARSET_HIGH.index(c) for c in row]
    assert idx == sorted(idx)
    assert idx[0] == 0 and idx[-1] > len(CHARSET_HIGH) * 0.9


def test_invert_reverses_mapping():
    conv = ASCIIConverter(width=10, charset=CHARSET_STANDARD, invert=True)
    assert set(conv.convert(np.zeros((20, 20), np.uint8))) == {"@", "\n"}
    assert set(conv.convert(np.full((20, 20), 255, np.uint8))) == {" ", "\n"}


def test_high_detail_charset_uses_many_levels():
    gradient = np.tile(np.linspace(0, 255, 256).astype(np.uint8), (8, 1))
    high = set(ASCIIConverter(width=256, charset=CHARSET_HIGH).convert(gradient)) - {"\n"}
    std = set(ASCIIConverter(width=256, charset=CHARSET_STANDARD).convert(gradient)) - {"\n"}
    assert len(high) > 3 * len(std)


def test_color_mode_emits_ansi_and_same_text():
    frame = np.zeros((20, 20, 3), np.uint8)
    frame[:, :10] = (0, 0, 255)  # red in BGR
    frame[:, 10:] = (255, 0, 0)  # blue
    plain = ASCIIConverter(width=20).convert(frame)
    colored = ASCIIConverter(width=20, color=True).convert(frame)
    assert "\x1b[38;2;255;0;0m" in colored
    assert "\x1b[38;2;0;0;255m" in colored
    assert ANSI.sub("", colored) == plain


def test_color_mode_on_gray_frame():
    out = ASCIIConverter(width=10, color=True).convert(np.full((20, 20), 128, np.uint8))
    assert "\x1b[38;2;" in out


@pytest.mark.parametrize("bad", [None, np.empty((0, 3), np.uint8), np.zeros((4, 4, 2), np.uint8), 42])
def test_invalid_frames_raise_cleanly(bad):
    with pytest.raises(InvalidFrameError):
        ASCIIConverter(width=10).convert(bad)


def test_invalid_charset():
    with pytest.raises(ValueError):
        ASCIIConverter(charset="x")


def test_unicode_charset():
    out = ASCIIConverter(width=10, charset=" ░▒▓█").convert(np.full((20, 20), 255, np.uint8))
    assert set(out) == {"█", "\n"}
