"""High-level playback controls on top of an audio backend and the shared clock."""

from __future__ import annotations

import threading
from collections.abc import Callable

from src.audio.audio_player import AudioBackend
from src.audio.playback_clock import PlaybackClock
from src.core.exceptions import AudioPlaybackError

MAX_VOLUME = 130  # mpv allows amplification above 100


class AudioController:
    """User-level operations: pause/resume, seek, volume, stop.

    Position and duration are always read from the :class:`PlaybackClock`, never calculated
    here. Control calls are serialised by a lock so key-repeat cannot interleave commands.
    """

    def __init__(self, backend: AudioBackend, clock: PlaybackClock, volume: int = 80,
                 volume_step: int = 5, seek_step: float = 5.0) -> None:
        self._backend = backend
        self.clock = clock
        self._volume = max(0, min(MAX_VOLUME, volume))
        self._volume_step = volume_step
        self._seek_step = seek_step
        self._lock = threading.Lock()
        self._running = False
        self._want_paused = True  # mpv is started paused

    # -- state

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def paused(self) -> bool:
        """Requested pause state (applied to mpv immediately; the clock follows via events)."""
        return self._want_paused

    @property
    def position(self) -> float:
        return self.clock.position()

    @property
    def duration(self) -> float | None:
        return self.clock.duration

    @property
    def ended(self) -> bool:
        return self.clock.ended

    @property
    def error(self) -> str | None:
        return self.clock.error

    # -- lifecycle

    def load(self, source: str, user_agent: str | None = None) -> None:
        """Prepare *source*; playback stays paused until :meth:`resume`."""
        with self._lock:
            self._backend.start(source, self._volume, user_agent)
            self._running = True

    def stop(self) -> None:
        """Stop playback and release the engine."""
        with self._lock:
            if self._running:
                self._running = False
                self._backend.stop()

    # -- controls

    def pause(self) -> None:
        self._set_pause(True)

    def resume(self) -> None:
        self._set_pause(False)

    def toggle_pause(self) -> bool:
        """Toggle and return the new paused state."""
        self._set_pause(not self._want_paused)
        return self._want_paused

    def _set_pause(self, paused: bool) -> None:
        self._guarded(lambda: self._backend.set_pause(paused))
        self._want_paused = paused

    def seek(self, delta: float) -> None:
        """Seek relative to the current position (negative = backward)."""
        self._guarded(lambda: self._backend.seek_relative(delta))

    def seek_forward(self) -> None:
        self.seek(self._seek_step)

    def seek_backward(self) -> None:
        self.seek(-self._seek_step)

    def set_volume(self, volume: int) -> int:
        """Set the volume (clamped to 0-130) and return the applied value."""
        value = max(0, min(MAX_VOLUME, int(volume)))
        self._guarded(lambda: self._backend.set_volume(value))
        self._volume = value
        return value

    def volume_up(self) -> int:
        return self.set_volume(self._volume + self._volume_step)

    def volume_down(self) -> int:
        return self.set_volume(self._volume - self._volume_step)

    def _guarded(self, action: Callable[[], None]) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                action()
            except AudioPlaybackError as exc:
                if not self.clock.ended:  # a key pressed after a normal end is not an error
                    self.clock.set_error(str(exc))
