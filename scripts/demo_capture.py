"""Render the real player screen to plain text (for the README "terminal demonstration").

This runs the actual UI code (``build_player_view`` + ``ASCIIConverter``) on a frame of the
bundled synthetic clip with example lyric lines. It is a text rendering of the interface, not a
screenshot of a live session, and it uses no copyrighted media.

    python scripts/demo_capture.py            # prints the screen
    python scripts/demo_capture.py --loading  # prints the loading card
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402

from src.core.config import Quality  # noqa: E402
from src.lyrics.lyrics_models import LyricsWindow  # noqa: E402
from src.ui.player_view import (  # noqa: E402
    ViewState,
    build_loading_view,
    build_player_view,
    video_area,
)
from src.video.ascii_converter import ASCIIConverter  # noqa: E402
from src.video.video_loader import VideoLoader  # noqa: E402


def capture(width: int = 100, height: int = 30, loading: bool = False) -> str:
    console = Console(file=io.StringIO(), force_terminal=False, width=width, height=height,
                      record=True, legacy_windows=False)
    if loading:
        state = ViewState(title="Example Song", artist="Example Artist", loading="x", spinner=3,
                          loading_steps=[("Media information", "done"), ("Video stream", "done"),
                                         ("Audio engine", "done"), ("Lyrics", "active")])
        console.print(build_loading_view(state))
        return console.export_text()
    with VideoLoader(ROOT / "assets" / "samples" / "demo.avi") as loader:
        for _ in range(30):
            loader.read()
        _, frame = loader.read()
    cols, rows = video_area(width, height, show_lyrics=True)
    quality = Quality.BALANCED
    profile = quality.profile
    converter = ASCIIConverter(width=min(profile.max_width, cols), charset=quality.charset, max_rows=rows,
                               interpolation=profile.interpolation, stabilize=True, chunk=profile.chunk,
                               enhance=profile.enhance)
    state = ViewState(
        title="Example Song", artist="Example Artist", origin="Local file", position=84, duration=252,
        paused=False, volume=80, ascii_frame=converter.convert(frame),
        lyrics=LyricsWindow(("Previous lyric line",), ("Current lyric line is highlighted",),
                            ("Next lyric line",), 3))
    console.print(build_player_view(state, width))
    return console.export_text()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loading", action="store_true")
    ap.add_argument("--width", type=int, default=100)
    ap.add_argument("--height", type=int, default=30)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    print(capture(a.width, a.height, a.loading))
