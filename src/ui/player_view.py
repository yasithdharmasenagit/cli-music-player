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
# Picture-first ("compact") layout: borderless one-line header, 3 lyric lines, 3 footer lines.
COMPACT_HEADER_ROWS = 1
COMPACT_LYRICS_ROWS = 3
COMPACT_FOOTER_ROWS = 3

HINTS = "SPACE pause · ←/→ seek · ↑/↓ volume · 1/2/3 quality · L lyrics · V video · Q quit"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
VOLUME_SEGMENTS = 10


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
    loading: str | None = None            # set while the loading screen is shown
    #: Loading checklist: ``(label, status)`` with status done | active | fail | skip.
    loading_steps: list[tuple[str, str]] = field(default_factory=list)
    spinner: int = 0
    ascii_frame: str | None = None
    color: bool = False
    audio_only: bool = False
    video_notice: str = ""                # e.g. "Video unavailable: audio only"
    show_video: bool = True
    show_lyrics: bool = True
    lyrics: LyricsWindow = field(default_factory=LyricsWindow)
    lyrics_message: str = ""
    quality: str = ""                     # current quality name (low / medium / high)
    compact: bool = False                 # picture-first layout (used by the high quality)
    notice: str = ""                      # transient message shown in the footer
    notice_kind: str = "info"             # info | warn
    dropped_note: str = ""


def video_area(width: int, height: int, show_lyrics: bool, compact: bool = False) -> tuple[int, int]:
    """``(cols, rows)`` available for ASCII art (inside the video panel, or the whole middle
    of the screen in the compact layout)."""
    if compact:
        used = COMPACT_HEADER_ROWS + COMPACT_FOOTER_ROWS + (COMPACT_LYRICS_ROWS if show_lyrics else 0)
        return max(1, width), max(1, height - used)
    used = HEADER_ROWS + FOOTER_ROWS + (LYRICS_ROWS if show_lyrics else 0) + PANEL_CHROME_ROWS
    return max(1, width - PANEL_CHROME_COLS), max(1, height - used)


def progress_bar(position: float, duration: float | None, width: int) -> str:
    """``[=====-----]`` bar; empty when the duration is unknown."""
    inner = max(1, width - 2)
    if not duration or duration <= 0:
        return "[" + "-" * inner + "]"
    filled = max(0, min(inner, int(round(inner * position / duration))))
    return "[" + "=" * filled + "-" * (inner - filled) + "]"


def volume_bar(volume: int) -> str:
    """Ten-segment volume meter (100% fills it; amplified volumes keep it full)."""
    filled = max(0, min(VOLUME_SEGMENTS, round(volume / 100 * VOLUME_SEGMENTS)))
    return "█" * filled + "░" * (VOLUME_SEGMENTS - filled)


def _progress_text(position: float, duration: float | None, width: int) -> Text:
    bar = progress_bar(position, duration, width)
    text = Text(no_wrap=True, overflow="crop")
    text.append("[", style=PALETTE.dim)
    inner = bar[1:-1]
    filled = inner.count("=")
    text.append("━" * filled, style=PALETTE.accent)
    text.append("─" * (len(inner) - filled), style=PALETTE.dim)
    text.append("]", style=PALETTE.dim)
    return text


def _header(state: ViewState) -> Panel:
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append("ASCII MUSIC PLAYER\n", style=PALETTE.accent)
    text.append("NOW PLAYING  ", style=PALETTE.dim)
    text.append(state.title or "…", style="bold " + PALETTE.text)
    if state.artist:
        text.append(f"  —  {state.artist}", style=PALETTE.text)
    if state.origin:
        text.append(f"   [{state.origin}]", style=PALETTE.dim)
    return Panel(text, box=box.ROUNDED, border_style=PALETTE.accent_alt, padding=(0, 1))


def _video(state: ViewState) -> Panel:
    body: RenderableType
    if state.audio_only or state.ascii_frame is None:
        msg = state.video_notice or ("♪  Audio only" if state.audio_only else "…")
        body = Align.center(Text(msg, style=PALETTE.dim), vertical="middle")
    elif state.color:
        body = Align.center(Text.from_ansi(state.ascii_frame, no_wrap=True, overflow="crop"),
                            vertical="middle")
    else:
        body = Align.center(Text(state.ascii_frame, style=PALETTE.ascii_art, no_wrap=True,
                                 overflow="crop"), vertical="middle")
    return Panel(body, box=box.ROUNDED, border_style=PALETTE.dim, padding=0)


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
    line1 = Text(no_wrap=True, overflow="ellipsis")
    line1.append(f"{format_time(state.position)} / {total}", style=PALETTE.text)
    line1.append("   " + status, style=PALETTE.accent)
    if state.quality:
        line1.append(f"   quality {state.quality}", style=PALETTE.dim)
    if state.notice:
        line1.append("   " + state.notice,
                     style=PALETTE.warning if state.notice_kind == "warn" else PALETTE.accent_alt)
    line2 = _progress_text(state.position, state.duration, inner)
    line3 = Text(no_wrap=True, overflow="ellipsis")
    line3.append("Volume ", style=PALETTE.dim)
    line3.append(volume_bar(state.volume), style=PALETTE.accent_alt)
    line3.append(f" {state.volume}%", style=PALETTE.text)
    line3.append("   " + HINTS, style=PALETTE.dim)
    return Panel(Group(line1, line2, line3), box=box.ROUNDED, border_style=PALETTE.accent_alt,
                 padding=(0, 1))


def _compact_header(state: ViewState) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append("ASCII MUSIC PLAYER", style=PALETTE.accent)
    text.append("  ·  ", style=PALETTE.dim)
    text.append(state.title or "…", style="bold " + PALETTE.text)
    if state.artist:
        text.append(f" — {state.artist}", style=PALETTE.text)
    if state.origin:
        text.append(f"  [{state.origin}]", style=PALETTE.dim)
    return text


def _compact_picture(state: ViewState) -> RenderableType:
    if state.audio_only or state.ascii_frame is None:
        msg = state.video_notice or ("♪  Audio only" if state.audio_only else "…")
        return Align.center(Text(msg, style=PALETTE.dim), vertical="middle")
    if state.color:
        art = Text.from_ansi(state.ascii_frame, no_wrap=True, overflow="crop")
    else:
        art = Text(state.ascii_frame, style=PALETTE.ascii_art, no_wrap=True, overflow="crop")
    return Align.center(art, vertical="middle")


def _compact_lyrics(state: ViewState) -> RenderableType:
    w = state.lyrics
    if state.lyrics_message and not (w.previous or w.current or w.upcoming):
        return Align.center(Text(state.lyrics_message, style=PALETTE.dim), vertical="middle")
    current = _lyric_line(w.current, "bold " + PALETTE.accent) if w.current else         Text("♪", style=PALETTE.dim, justify="center")
    return Group(_lyric_line(w.previous, PALETTE.dim), current, _lyric_line(w.upcoming, PALETTE.text))


def _compact_footer(state: ViewState, width: int) -> RenderableType:
    total = format_time(state.duration) if state.duration else "--:--"
    status = "‖ Paused" if state.paused else "▶ Playing"
    line1 = Text(no_wrap=True, overflow="ellipsis")
    line1.append(f"{format_time(state.position)} / {total}", style=PALETTE.text)
    line1.append("  " + status, style=PALETTE.accent)
    if state.quality:
        line1.append(f"  quality {state.quality}", style=PALETTE.dim)
    line1.append("  Vol ", style=PALETTE.dim)
    line1.append(volume_bar(state.volume), style=PALETTE.accent_alt)
    line1.append(f" {state.volume}%", style=PALETTE.text)
    if state.notice:
        line1.append("  " + state.notice,
                     style=PALETTE.warning if state.notice_kind == "warn" else PALETTE.accent_alt)
    line2 = _progress_text(state.position, state.duration, max(10, width))
    line3 = Text(HINTS, style=PALETTE.dim, no_wrap=True, overflow="ellipsis")
    return Group(line1, line2, line3)


def _build_compact_view(state: ViewState, width: int) -> Layout:
    """Picture-first layout: no borders, the picture takes everything the thin bars leave."""
    root = Layout()
    sections: list[Layout] = [Layout(_compact_header(state), name="header", size=COMPACT_HEADER_ROWS)]
    if state.show_video:
        sections.append(Layout(_compact_picture(state), name="video", ratio=1))
    if state.show_lyrics:
        sections.append(Layout(_compact_lyrics(state), name="lyrics",
                               size=COMPACT_LYRICS_ROWS if state.show_video else None, ratio=1))
    if not state.show_video and not state.show_lyrics:
        sections.append(Layout(Align.center(Text("Video and lyrics hidden (V / L)", style=PALETTE.dim),
                                            vertical="middle"), name="body", ratio=1))
    sections.append(Layout(_compact_footer(state, width), name="footer", size=COMPACT_FOOTER_ROWS))
    root.split_column(*sections)
    return root


def build_player_view(state: ViewState, width: int) -> Layout:
    """Assemble the full-screen layout for the current state."""
    if state.compact:
        return _build_compact_view(state, width)
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


def build_loading_view(state: ViewState) -> Layout:
    """Centered loading card with a spinner and a live checklist."""
    rows: list[RenderableType] = [
        Text("ASCII MUSIC PLAYER", style=PALETTE.accent, justify="center"),
        Text("Watch music. Differently.", style=PALETTE.dim, justify="center"),
        Text(""),
        Text(state.title or "…", style="bold " + PALETTE.text, justify="center", no_wrap=True,
             overflow="ellipsis"),
    ]
    if state.artist:
        rows.append(Text(state.artist, style=PALETTE.text, justify="center", no_wrap=True,
                         overflow="ellipsis"))
    rows.append(Text(""))
    spin = SPINNER[state.spinner % len(SPINNER)]
    for label, status in state.loading_steps:
        line = Text(no_wrap=True, overflow="ellipsis")
        if status == "done":
            line.append("✓ ", style=PALETTE.accent)
            line.append(label, style=PALETTE.text)
        elif status == "active":
            line.append(f"{spin} ", style=PALETTE.accent_alt)
            line.append(label + "…", style=PALETTE.text)
        elif status == "fail":
            line.append("✗ ", style=PALETTE.warning)
            line.append(label + " (unavailable)", style=PALETTE.dim)
        else:  # skip
            line.append("– ", style=PALETTE.dim)
            line.append(label, style=PALETTE.dim)
        rows.append(line)
    rows.append(Text(""))
    rows.append(Text("Q to cancel", style=PALETTE.dim, justify="center"))
    card = Panel(Group(*rows), box=box.ROUNDED, border_style=PALETTE.accent_alt,
                 padding=(1, 4), width=58)
    root = Layout()
    root.update(Align.center(card, vertical="middle"))
    return root
