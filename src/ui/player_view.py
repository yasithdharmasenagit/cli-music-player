"""Combined player screen: header, ASCII video, lyrics and transport footer (pure builders)."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich import box
from rich.align import Align
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

from src.lyrics.lyrics_models import LyricsWindow
from src.ui.colors import PALETTE
from src.ui.layout import format_time

HEADER_ROWS = 4   # 2 text lines + borders
LYRICS_ROWS = 5   # 3 text lines + borders
FOOTER_ROWS = 5   # 3 text lines + borders
PANEL_CHROME_COLS = 2
PANEL_CHROME_ROWS = 2

HINTS = "SPACE pause · ←/→ seek · ↑/↓ volume · L lyrics · V video · Q quit"


@dataclass(slots=True)
class ViewState:
    """Everything the screen shows; the session mutates it and re-renders."""

    title: str = ""
    artist: str | None = None
    origin: str = ""
    position: float = 0.0
    duration: float | None = None
    paused: bool = True
    volume: int = 80
    loading: str | None = None            # e.g. "Preparing playback…"
    ascii_frame: str | None = None
    color: bool = False
    audio_only: bool = False
    video_notice: str = ""                # e.g. "Video unavailable — audio only"
    show_video: bool = True
    show_lyrics: bool = True
    lyrics: LyricsWindow = field(default_factory=LyricsWindow)
    lyrics_message: str = ""
    dropped_note: str = ""


def video_area(width: int, height: int, show_lyrics: bool) -> tuple[int, int]:
    """``(cols, rows)`` available for ASCII art inside the video panel."""
    used = HEADER_ROWS + FOOTER_ROWS + (LYRICS_ROWS if show_lyrics else 0) + PANEL_CHROME_ROWS
    return max(1, width - PANEL_CHROME_COLS), max(1, height - used)


def progress_bar(position: float, duration: float | None, width: int) -> str:
    """``[=====-----]`` bar; empty when the duration is unknown."""
    inner = max(1, width - 2)
    if not duration or duration <= 0:
        return "[" + "-" * inner + "]"
    filled = max(0, min(inner, int(round(inner * position / duration))))
    return "[" + "=" * filled + "-" * (inner - filled) + "]"


def _header(state: ViewState) -> Panel:
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append("ASCII MUSIC PLAYER\n", style=PALETTE.accent)
    text.append("NOW PLAYING: ", style=PALETTE.dim)
    text.append(state.title or "…", style="bold " + PALETTE.text)
    if state.artist:
        text.append(f"  —  {state.artist}", style=PALETTE.text)
    if state.origin:
        text.append(f"   [{state.origin}]", style=PALETTE.dim)
    return Panel(text, box=box.ROUNDED, border_style=PALETTE.accent_alt, padding=(0, 1))


def _video(state: ViewState) -> Panel:
    body: RenderableType
    if state.loading:
        body = Align.center(Text(state.loading, style=PALETTE.accent), vertical="middle")
    elif state.audio_only or state.ascii_frame is None:
        msg = state.video_notice or ("♪  Audio only" if state.audio_only else "…")
        body = Align.center(Text(msg, style=PALETTE.dim), vertical="middle")
    elif state.color:
        body = Align.center(Text.from_ansi(state.ascii_frame, no_wrap=True, overflow="crop"),
                            vertical="middle")
    else:
        body = Align.center(Text(state.ascii_frame, style=PALETTE.ascii_art, no_wrap=True,
                                 overflow="crop"), vertical="middle")
    return Panel(body, box=box.ROUNDED,
                 border_style=PALETTE.dim, padding=0)


def _lyric_line(parts: tuple[str, ...], style: str) -> Text:
    return Text(" · ".join(parts), style=style, no_wrap=True, overflow="ellipsis",
                justify="center")


def _lyrics(state: ViewState) -> Panel:
    w = state.lyrics
    if state.lyrics_message and not (w.previous or w.current or w.upcoming):
        body: RenderableType = Align.center(Text(state.lyrics_message, style=PALETTE.dim),
                                            vertical="middle")
    else:
        current = _lyric_line(w.current, "bold " + PALETTE.accent) if w.current else \
            Text("♪", style=PALETTE.dim, justify="center")
        body = Group(_lyric_line(w.previous, PALETTE.dim), current,
                     _lyric_line(w.upcoming, PALETTE.text))
    return Panel(body, box=box.ROUNDED, border_style=PALETTE.dim, padding=0, title="LYRICS",
                 title_align="left")


def _footer(state: ViewState, width: int) -> Panel:
    inner = max(10, width - 4)
    total = format_time(state.duration) if state.duration else "--:--"
    status = "‖ Paused" if state.paused else "▶ Playing"
    if state.loading:
        status = "… Loading"
    line1 = Text(no_wrap=True, overflow="ellipsis")
    line1.append(f"{format_time(state.position)} / {total}", style=PALETTE.text)
    line1.append("   " + status, style=PALETTE.accent)
    line2 = Text(progress_bar(state.position, state.duration, inner), style=PALETTE.accent_alt,
                 no_wrap=True, overflow="crop")
    line3 = Text(no_wrap=True, overflow="ellipsis")
    line3.append(f"Volume: {state.volume}%", style=PALETTE.text)
    line3.append("   " + HINTS, style=PALETTE.dim)
    return Panel(Group(line1, line2, line3), box=box.ROUNDED, border_style=PALETTE.accent_alt,
                 padding=(0, 1))


def build_player_view(state: ViewState, width: int) -> Layout:
    """Assemble the full-screen layout for the current state."""
    root = Layout()
    sections: list[Layout] = [Layout(_header(state), name="header", size=HEADER_ROWS)]
    if state.show_video:
        sections.append(Layout(_video(state), name="video", ratio=1))
    if state.show_lyrics:
        sections.append(Layout(_lyrics(state), name="lyrics",
                               size=LYRICS_ROWS if state.show_video else None, ratio=1))
    if not state.show_video and not state.show_lyrics:
        sections.append(Layout(Panel(Align.center(Text("Video and lyrics hidden (V / L)",
                                                        style=PALETTE.dim), vertical="middle"),
                                     box=box.ROUNDED, border_style=PALETTE.dim),
                               name="body", ratio=1))
    sections.append(Layout(_footer(state, width), name="footer", size=FOOTER_ROWS))
    root.split_column(*sections)
    return root
