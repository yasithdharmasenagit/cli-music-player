from __future__ import annotations

import io

import pytest
from rich.console import Console

from src.lyrics.lyrics_models import LyricsWindow
from src.ui.keyboard import Key, KeyReader, decode_ansi, decode_windows
from src.ui.player_view import (
    HEADER_ROWS,
    ViewState,
    build_loading_view,
    build_player_view,
    progress_bar,
    video_area,
    volume_bar,
)
from src.video.video_loader import VideoLoader
from src.video.video_sync import (
    SyncAction,
    SyncedVideoSource,
    plan_sync,
    target_frame_index,
)

# ------------------------------------------------------------------ pure sync maths


def test_target_frame_index():
    assert target_frame_index(0.0, 30) == 0
    assert target_frame_index(1.0, 30) == 30
    assert target_frame_index(0.0333, 30) == 0 and target_frame_index(0.0334, 30) == 1
    assert target_frame_index(100.0, 30, frame_count=90) == 89  # clamp to last frame
    assert target_frame_index(-3.0, 30) == 0
    assert target_frame_index(float("nan"), 30) == 0
    assert target_frame_index(5.0, 0) == 0


def test_plan_sync_decisions():
    # loader will next read frame 10, so frame 9 is on screen
    assert plan_sync(9, 10).action is SyncAction.HOLD
    assert plan_sync(10, 10).action is SyncAction.READ
    p = plan_sync(14, 10)
    assert p.action is SyncAction.SKIP and p.skip == 4
    assert plan_sync(10 + 24, 10).action is SyncAction.SKIP
    assert plan_sync(10 + 25, 10).action is SyncAction.SEEK
    assert plan_sync(10 - 1 - 100, 10).action is SyncAction.SEEK  # real seek backward
    assert plan_sync(3, 10).action is SyncAction.HOLD  # small clock jitter is ignored
    assert plan_sync(9 - 15, 10).action is SyncAction.HOLD
    assert plan_sync(9 - 16, 10).action is SyncAction.SEEK
    assert plan_sync(0, 0).action is SyncAction.READ  # fresh start


def test_plan_sync_never_accumulates_delay():
    # simulate a renderer that takes 3 frame-times per frame; it must keep landing on target
    next_index, shown = 0, []
    for step in range(10):
        target = step * 3
        plan = plan_sync(target, next_index)
        if plan.action is SyncAction.SKIP:
            next_index += plan.skip
        next_index += 1
        shown.append(next_index - 1)
    assert shown == [i * 3 for i in range(10)]


# ------------------------------------------------------------------ synced source on real file


def test_synced_source_follows_clock(make_video):
    path = make_video(frames=40, fps=10.0)
    with VideoLoader(path) as loader:
        src = SyncedVideoSource(loader)
        idx, frame = src.frame_at(0.0)
        assert idx == 0 and frame.shape == (48, 64, 3)
        assert src.frame_at(0.05) is None                       # same frame: hold
        assert src.frame_at(0.1)[0] == 1                         # next frame
        assert src.frame_at(0.45)[0] == 4                        # skipped 2, 3
        assert src.frames_skipped == 2
        assert src.frame_at(3.0)[0] == 30                        # big gap: seek
        assert src.seeks == 1
        assert src.frame_at(0.5)[0] == 5                         # backward: seek
        assert src.seeks == 2 and src.shown_index == 5


def test_synced_source_end_of_video(make_video):
    with VideoLoader(make_video(frames=30, fps=10.0)) as loader:
        src = SyncedVideoSource(loader)
        assert src.frame_at(2.9)[0] == 29
        assert src.frame_at(99.0) is None  # clamped to the last frame, already shown
        # seeking back far enough revives a source that hit EOF
        assert src.frame_at(0.1)[0] == 1 and not src.exhausted


def test_synced_source_ignores_clock_jitter(make_video):
    with VideoLoader(make_video(frames=40, fps=10.0)) as loader:
        src = SyncedVideoSource(loader)
        assert src.frame_at(1.0)[0] == 10
        assert src.frame_at(0.8) is None and src.frame_at(0.95) is None  # clock stepped back a bit
        assert src.seeks == 0 and src.shown_index == 10
        assert src.frame_at(1.1)[0] == 11


def test_synced_source_seek_failure_returns_none(make_video):
    with VideoLoader(make_video(frames=40, fps=10.0)) as loader:
        src = SyncedVideoSource(loader)
        src.frame_at(0.0)
        loader.seek = lambda i: False  # type: ignore[method-assign]
        loader.reopen = lambda i: False  # type: ignore[method-assign]
        assert src.frame_at(3.0) is None and src.recovering and not src.exhausted


# ------------------------------------------------------------------ keyboard


class ListChars:
    def __init__(self, chars):
        self.chars = list(chars)

    def has_char(self):
        return bool(self.chars)

    def get_char(self):
        return self.chars.pop(0)


def test_windows_key_decoding():
    chars = [" ", "q", "Q", "l", "L", "v", "V", "\x03", "\xe0", "K", "\xe0", "M", "\xe0", "H",
             "\x00", "P", "x", "\xe0", "Z"]
    keys = KeyReader(ListChars(chars), windows=True).poll(limit=50)
    assert keys == [Key.PAUSE, Key.QUIT, Key.QUIT, Key.TOGGLE_LYRICS, Key.TOGGLE_LYRICS,
                    Key.TOGGLE_VIDEO, Key.TOGGLE_VIDEO, Key.QUIT, Key.SEEK_BACK, Key.SEEK_FORWARD,
                    Key.VOLUME_UP, Key.VOLUME_DOWN]


def test_ansi_key_decoding_and_poll_limit():
    assert decode_ansi("\x1b[D") == Key.SEEK_BACK and decode_ansi("\x1bOC") == Key.SEEK_FORWARD
    assert decode_ansi("\x1b[A") == Key.VOLUME_UP and decode_ansi("\x1b[B") == Key.VOLUME_DOWN
    assert decode_ansi(" ") == Key.PAUSE and decode_ansi("z") is None and decode_ansi("\x1b") is None
    assert decode_windows("a") is None
    reader = KeyReader(ListChars([" "] * 100), windows=True)
    assert len(reader.poll(limit=16)) == 16  # bounded per call, so the render loop never starves
    assert KeyReader(None).poll() == []      # no terminal: no keys, no crash


# ------------------------------------------------------------------ view


def render_text(state, width=100, height=30):
    console = Console(width=width, height=height, force_terminal=False, record=True,
                      file=io.StringIO())
    console.print(build_player_view(state, width))
    return console.export_text()


def test_video_area_adapts_to_terminal_and_panels():
    assert video_area(100, 40, True) == (98, 40 - HEADER_ROWS - 5 - 5 - 2)
    assert video_area(100, 40, False)[1] == video_area(100, 40, True)[1] + 5
    assert video_area(1, 1, True) == (1, 1)  # tiny terminals never produce zero/negative sizes


def test_volume_bar():
    assert volume_bar(0) == "░" * 10 and volume_bar(100) == "█" * 10
    assert volume_bar(50) == "█" * 5 + "░" * 5
    assert volume_bar(130) == "█" * 10 and volume_bar(-5) == "░" * 10


def test_progress_bar():
    assert progress_bar(0, 100, 12) == "[" + "-" * 10 + "]"
    assert progress_bar(50, 100, 12) == "[=====-----]"
    assert progress_bar(100, 100, 12) == "[" + "=" * 10 + "]"
    assert progress_bar(500, 100, 12).count("=") == 10  # clamped
    assert progress_bar(5, None, 12) == "[" + "-" * 10 + "]"


def test_view_shows_all_sections():
    state = ViewState(title="Song", artist="Band", origin="Local file", position=84, duration=252,
                      paused=False, volume=80, ascii_frame="@@@@\n####",
                      lyrics=LyricsWindow(("before",), ("NOW LINE",), ("after",), 3))
    text = render_text(state)
    for needle in ["ASCII MUSIC PLAYER", "NOW PLAYING", "Song", "Band", "@@@@", "before",
                   "NOW LINE", "after", "01:24 / 04:12", "Playing", "Volume", "80%", "[", "━"]:
        assert needle in text, needle


def test_view_states():
    loading = ViewState(title="Song", artist="Band", loading="x", loading_steps=[
        ("Media information", "done"), ("Audio engine", "active"), ("Lyrics", "fail"),
        ("Video stream", "skip")])
    console = Console(width=80, height=24, force_terminal=False, record=True, file=io.StringIO())
    console.print(build_loading_view(loading))
    card = console.export_text()
    for needle in ["ASCII MUSIC PLAYER", "Watch music. Differently.", "Song", "Band", "✓ Media information",
                   "Audio engine…", "✗ Lyrics (unavailable)", "Q to cancel"]:
        assert needle in card, needle
    notice = render_text(ViewState(title="t", notice="Video stream interrupted", notice_kind="warn"))
    assert "Video stream interrupted" in notice
    assert "Paused" in render_text(ViewState(title="t", paused=True))
    assert "Audio only" in render_text(ViewState(title="t", audio_only=True))
    assert "Searching for lyrics" in render_text(ViewState(title="t", lyrics_message="Searching for lyrics…"))
    hidden = render_text(ViewState(title="t", show_video=False, show_lyrics=False))
    assert "hidden" in hidden
    no_video = render_text(ViewState(title="t", ascii_frame="@@@@", show_video=False))
    assert "@@@@" not in no_video and "LYRICS" in no_video
    no_lyrics = render_text(ViewState(title="t", show_lyrics=False))
    assert "LYRICS" not in no_lyrics


@pytest.mark.parametrize("size", [(20, 8), (200, 60), (80, 24)])
def test_view_survives_any_terminal_size(size):
    render_text(ViewState(title="x" * 300, artist="y" * 300, ascii_frame="@" * 500,
                          lyrics=LyricsWindow(("a" * 400,), ("b" * 400,), ("c" * 400,))),
                width=size[0], height=size[1])


def test_quality_keys_decode_on_windows_and_posix():
    assert KeyReader(ListChars(["1", "2", "3"]), windows=True).poll() == [
        Key.QUALITY_LOW, Key.QUALITY_MEDIUM, Key.QUALITY_HIGH]
    assert KeyReader(ListChars(["1", "2", "3"]), windows=False).poll() == [
        Key.QUALITY_LOW, Key.QUALITY_MEDIUM, Key.QUALITY_HIGH]
    assert KeyReader(ListChars(["4", "0"]), windows=True).poll() == []  # other digits are ignored


def test_footer_shows_quality_and_the_hint_for_the_keys():
    text = render_text(ViewState(title="t", quality="medium", paused=False), width=120)
    assert "quality medium" in text and "1/2/3 quality" in text
