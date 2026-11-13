"""Rich Live based frame sink."""

from __future__ import annotations

from rich.console import Console
from rich.live import Live

from src.core.config import AppConfig
from src.ui.layout import build_frame_view
from src.video.video_player import PlaybackStatus


class TerminalRenderer:
    """Draws ASCII frames with ``rich.live.Live`` (implements ``FrameSink``).

    Uses the alternate screen buffer (configurable) so the user's scrollback is restored
    untouched when playback ends, and manual refresh so each frame is one flush.
    """

    def __init__(self, config: AppConfig, console: Console | None = None) -> None:
        self._config = config
        self.console = console or Console()
        self._live: Live | None = None

    def start(self) -> None:
        """Enter live rendering mode."""
        if self._live is not None:
            return
        self._live = Live(
            console=self.console,
            screen=self._config.alt_screen,
            auto_refresh=False,
            transient=not self._config.alt_screen,
            vertical_overflow="crop",
        )
        self._live.start()

    def draw(self, ascii_frame: str, status: PlaybackStatus) -> None:
        """Display one frame."""
        if self._live is None:
            self.start()
        assert self._live is not None
        view = build_frame_view(ascii_frame, status if self._config.show_stats else None,
                                self._config.color)
        self._live.update(view, refresh=True)

    def stop(self) -> None:
        """Leave live mode and restore the terminal. Safe to call repeatedly."""
        live, self._live = self._live, None
        if live is not None:
            live.stop()

    def available_rows(self, reserved: int = 1) -> int:
        """Terminal rows available for the picture (minus *reserved* status rows)."""
        return max(1, self.console.size.height - (reserved if self._config.show_stats else 0) - 1)

    def available_cols(self) -> int:
        """Terminal columns available."""
        return max(1, self.console.size.width)
