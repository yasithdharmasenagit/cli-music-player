"""Quality means detail: more characters on screen, richer ramps, enhanced contrast."""

from __future__ import annotations

import io

import cv2
import numpy as np
import pytest
from rich.cells import cell_len
from rich.console import Console

from src.core.config import AppConfig, Quality
from src.lyrics.lyrics_models import LyricsWindow
from src.ui.player_view import (
    COMPACT_FOOTER_ROWS,
    COMPACT_HEADER_ROWS,
    COMPACT_LYRICS_ROWS,
    ViewState,
    build_player_view,
    video_area,
)
from src.video.ascii_converter import ASCIIConverter
from tests.test_lifecycle import make_session


def scene(h=360, w=640, seed=5):
    """A textured test picture with gradients, edges and a bright object."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 3), np.uint8)
    img[..., 0] = (80 + 60 * np.sin(xx / 29.0)).astype(np.uint8)
    img[..., 1] = (90 + 70 * np.sin(yy / 17.0)).astype(np.uint8)
    img[..., 2] = (100 + 50 * np.sin((xx + yy) / 41.0)).astype(np.uint8)
    cv2.circle(img, (w // 2, h // 2), h // 4, (230, 230, 230), -1)
    cv2.putText(img, "DETAIL", (w // 6, h // 5), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
    return cv2.add(img, rng.integers(0, 12, img.shape, dtype=np.uint8))


def converter_for(quality: Quality, width=120, height=40, show_lyrics=True, **kw):
    cfg = AppConfig(quality=quality)
    cols, rows = video_area(width, height, show_lyrics, cfg.compact)
    return ASCIIConverter(width=min(cfg.effective_width, cols), charset=cfg.active_charset, max_rows=rows,
                          interpolation=cfg.interpolation, stabilize=True, chunk=cfg.chunk,
                          enhance=cfg.enhance, **kw)


# ------------------------------------------------------------------ profiles


def test_profiles_define_detail_not_just_a_character_set():
    low, med, high = (q.profile for q in (Quality.LOW, Quality.BALANCED, Quality.HIGH))
    assert (low.chunk, med.chunk, high.chunk) == (2, 1, 1)
    assert (low.enhance, med.enhance, high.enhance) == ("none", "levels", "detail")
    assert (low.compact, med.compact, high.compact) == (False, False, True)
    assert len(low.charset) < len(med.charset) < len(high.charset)
    assert low.max_width < med.max_width < high.max_width


# ------------------------------------------------------------------ picture size and samples


@pytest.mark.parametrize("window", [(100, 30), (120, 40), (160, 50), (220, 60)])
def test_each_quality_step_shows_more_characters_and_more_samples(window):
    """In any window size: low < medium < high, in both characters on screen and distinct samples."""
    frame = scene()
    stats = {}
    for q in Quality:
        conv = converter_for(q, *window)
        text = conv.convert(frame)
        lines = text.split("\n")
        chars = len(lines[0]) * len(lines)
        samples = chars // (conv.chunk ** 2)
        stats[q] = (chars, samples, len(set(text) - {"\n"}))
    low, med, high = stats[Quality.LOW], stats[Quality.BALANCED], stats[Quality.HIGH]
    assert low[1] < med[1] < high[1], stats          # real picture detail (independent samples)
    assert med[0] <= high[0] and low[0] <= med[0] * 1.05  # the picture never gets smaller going up
    assert low[2] < med[2] < high[2]                 # richer character ramp in use


def test_high_gives_the_picture_far_more_room_than_medium_in_a_normal_window():
    cols_m, rows_m = video_area(120, 40, True, compact=False)
    cols_h, rows_h = video_area(120, 40, True, compact=True)
    assert (cols_m, rows_m) == (118, 24) and (cols_h, rows_h) == (120, 33)
    frame = scene()
    med = converter_for(Quality.BALANCED).convert(frame).split("\n")
    high = converter_for(Quality.HIGH).convert(frame).split("\n")
    assert len(high[0]) * len(high) > 1.5 * len(med[0]) * len(med)  # at least 50 % more characters


# ------------------------------------------------------------------ low: chunky blocks


def test_chunked_output_is_made_of_2x2_blocks():
    conv = ASCIIConverter(width=60, charset=" .:-=+*#%@", chunk=2, max_rows=20)
    lines = conv.convert(scene()).split("\n")
    assert len(lines) % 2 == 0 and len(lines[0]) % 2 == 0
    for r in range(0, len(lines), 2):
        for c in range(0, len(lines[0]), 2):
            block = {lines[r][c], lines[r][c + 1], lines[r + 1][c], lines[r + 1][c + 1]}
            assert len(block) == 1, (r, c)


def test_chunk_keeps_the_requested_size_and_reports_it():
    plain = ASCIIConverter(width=60, max_rows=20)
    chunky = ASCIIConverter(width=60, max_rows=20, chunk=2)
    a, b = plain.convert(scene()).split("\n"), chunky.convert(scene()).split("\n")
    assert abs(len(a[0]) - len(b[0])) <= 2 and abs(len(a) - len(b)) <= 2
    assert chunky.width == 60 and chunky.output_size(640, 360) == (len(b[0]), len(b))


def test_chunking_works_in_colour_mode():
    conv = ASCIIConverter(width=40, color=True, chunk=2, max_rows=16)
    out = conv.convert(scene())
    assert "\x1b[38;2;" in out and len(out.split("\n")) % 2 == 0


def test_low_profile_is_cheaper_than_medium():
    """Low = linear resize + 2x2 blocks; it must cost less per frame than medium (area + enhancement)."""
    frame = scene(720, 1280)
    costs = {}
    for quality in (Quality.LOW, Quality.BALANCED):
        conv = converter_for(quality, 160, 50)
        for _ in range(5):
            conv.convert(frame)
        costs[quality] = min(_timed(conv, frame) for _ in range(5))  # best of 5: robust on a busy PC
    assert costs[Quality.LOW] < costs[Quality.BALANCED]


def _timed(conv, frame, n=15):
    import time

    t0 = time.perf_counter()
    for _ in range(n):
        conv.convert(frame)
    return time.perf_counter() - t0


# ------------------------------------------------------------------ enhancement


def dim_picture(low=70, high=110, h=180, w=320):
    """A low-contrast picture (dark and flat) whose details sit in a narrow brightness band."""
    rng = np.random.default_rng(2)
    base = np.tile(np.linspace(low, high, w, dtype=np.float32), (h, 1))
    base += 6 * np.sin(np.mgrid[0:h, 0:w][0] / 9.0)
    g = np.clip(base + rng.normal(0, 1.0, base.shape), 0, 255).astype(np.uint8)
    return np.dstack([g, g, g])


def test_levels_use_more_of_the_ramp_on_a_low_contrast_picture():
    frame = dim_picture()
    plain = ASCIIConverter(width=100, charset=Quality.BALANCED.charset)
    levels = ASCIIConverter(width=100, charset=Quality.BALANCED.charset, enhance="levels")
    for _ in range(25):  # let the range follower settle
        t_plain, t_levels = plain.convert(frame), levels.convert(frame)
    assert len(set(t_levels)) > len(set(t_plain)) + 4   # details spread over many more characters


def test_enhancement_does_not_pump_on_a_steady_picture():
    levels = ASCIIConverter(width=80, enhance="levels", stabilize=False)
    frames = [levels.convert(dim_picture()) for _ in range(12)]
    changed = [sum(a != b for a, b in zip(x, y)) for x, y in zip(frames[2:], frames[3:])]
    assert max(changed) == 0  # identical input gives an identical picture once settled


def test_enhancement_follows_a_scene_cut_quickly():
    levels = ASCIIConverter(width=80, charset=Quality.BALANCED.charset, enhance="levels")
    for _ in range(15):
        levels.convert(dim_picture(30, 60))
    outs = [len(set(levels.convert(dim_picture(150, 230)))) for _ in range(6)]
    assert outs[-1] >= 8 and outs[3] >= 8   # adapted within a few frames, not seconds


def test_detail_mode_adds_local_contrast():
    frame = scene()
    levels = ASCIIConverter(width=110, charset=CHARSET_HIGH_RAMP, enhance="levels", max_rows=30)
    detail = ASCIIConverter(width=110, charset=CHARSET_HIGH_RAMP, enhance="detail", max_rows=30)
    for _ in range(10):
        a, b = levels.convert(frame), detail.convert(frame)

    def edge_energy(text):
        rows = [[CHARSET_HIGH_RAMP.index(c) for c in line] for line in text.split("\n")]
        arr = np.array(rows, dtype=float)
        return float(np.abs(np.diff(arr, axis=1)).mean() + np.abs(np.diff(arr, axis=0)).mean())

    assert edge_energy(b) > edge_energy(a)


CHARSET_HIGH_RAMP = Quality.HIGH.charset


def test_invalid_detail_arguments_are_rejected():
    with pytest.raises(ValueError, match="chunk"):
        ASCIIConverter(chunk=0)
    with pytest.raises(ValueError, match="enhance"):
        ASCIIConverter(enhance="ultra")


def test_enhancement_state_is_reset_when_the_grid_changes():
    conv = ASCIIConverter(width=60, enhance="detail", max_rows=20)
    conv.convert(scene(360, 640))
    other = conv.convert(scene(640, 360))  # portrait video: different grid shape
    assert len(other.split("\n")) > 20 - 1


# ------------------------------------------------------------------ compact layout


def render(state, width=120, height=40):
    console = Console(file=io.StringIO(), width=width, height=height, force_terminal=False, record=True,
                      legacy_windows=False)
    console.print(build_player_view(state, width))
    text = console.export_text()
    lines = text.split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def test_compact_layout_geometry_is_exact_and_stable():
    conv = converter_for(Quality.HIGH)
    for i in range(5):
        state = ViewState(title="Song", artist="Band", origin="YouTube", position=84 + i, duration=219,
                          paused=False, quality="high", compact=True, ascii_frame=conv.convert(scene(seed=i)),
                          lyrics=LyricsWindow(("prev",), ("current line",), ("next",), 2))
        lines = render(state)
        assert len(lines) == 40
        assert {cell_len(line) for line in lines} == {120}


def test_compact_layout_contains_everything_the_panel_layout_does():
    state = ViewState(title="Song", artist="Band", origin="YouTube", position=84, duration=252,
                      paused=False, volume=65, quality="high", compact=True, ascii_frame="@@@@\n####",
                      lyrics=LyricsWindow(("before",), ("NOW LINE",), ("after",), 3),
                      notice="Quality: high")
    text = "\n".join(render(state))
    for needle in ("ASCII MUSIC PLAYER", "Song", "Band", "YouTube", "@@@@", "before", "NOW LINE", "after",
                   "01:24 / 04:12", "Playing", "quality high", "65%", "━", "1/2/3 quality", "Quality: high"):
        assert needle in text, needle
    assert "╭" not in text and "│" not in text   # borderless: the room goes to the picture


def test_compact_layout_states_and_hidden_panels():
    base = dict(title="t", compact=True, ascii_frame="@@@@")
    assert "‖ Paused" in "\n".join(render(ViewState(paused=True, **base)))
    assert "Audio only" in "\n".join(render(ViewState(title="t", compact=True, audio_only=True)))
    assert "hidden" in "\n".join(render(ViewState(show_video=False, show_lyrics=False, **base)))
    no_lyrics = render(ViewState(show_lyrics=False, **base))
    assert len(no_lyrics) == 40
    msg = "\n".join(render(ViewState(lyrics_message="No lyrics found.", **base)))
    assert "No lyrics found." in msg


def test_compact_video_area_matches_the_layout_constants():
    assert video_area(120, 40, True, True) == (120, 40 - COMPACT_HEADER_ROWS - COMPACT_FOOTER_ROWS
                                               - COMPACT_LYRICS_ROWS)
    assert video_area(120, 40, False, True)[1] == video_area(120, 40, True, True)[1] + COMPACT_LYRICS_ROWS
    assert video_area(1, 1, True, True) == (1, 1)


@pytest.mark.parametrize("size", [(20, 8), (200, 60), (80, 24)])
def test_compact_layout_survives_any_terminal_size(size):
    render(ViewState(title="x" * 300, artist="y" * 300, compact=True, ascii_frame="@" * 500,
                     lyrics=LyricsWindow(("a" * 400,), ("b" * 400,), ("c" * 400,))), *size)


# ------------------------------------------------------------------ live switching in a session


def test_switching_quality_while_playing_changes_the_picture_size(make_video):
    """Keys 1/2/3 must visibly change the amount of picture detail on screen."""
    video = make_video(frames=240, fps=30.0, size=(320, 180))
    script = [(0.5, "1"), (1.2, "2"), (1.9, "3"), (2.6, "q")]
    console = Console(file=io.StringIO(), force_terminal=True, width=140, height=44, color_system=None,
                      legacy_windows=False)
    session, mpv, _ = make_session(video, duration=30.0, script=script, console=console,
                                   config=AppConfig(quality=Quality.BALANCED, alt_screen=False))
    sizes: dict[str, tuple[int, int, int]] = {}
    original = session._render

    def record(loading=False):
        original(loading)
        frame = session._state.ascii_frame
        if not loading and frame and session._converter is not None:
            lines = frame.split("\n")
            sizes[session._state.quality] = (len(lines[0]), len(lines), session._converter.chunk)

    session._render = record
    assert session.run().quit_by_user
    assert set(sizes) == {"low", "medium", "high"}
    samples = {k: v[0] * v[1] // v[2] ** 2 for k, v in sizes.items()}
    assert samples["low"] < samples["medium"] < samples["high"], sizes
    assert sizes["high"][0] * sizes["high"][1] > 1.4 * sizes["medium"][0] * sizes["medium"][1]
    assert session._state.compact is True  # ended in high: picture-first layout
