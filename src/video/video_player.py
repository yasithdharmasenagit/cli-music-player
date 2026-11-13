"""Timed, frame-dropping video playback loop (UI-agnostic)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from src.core.config import AppConfig
from src.video.ascii_converter import ASCIIConverter
from src.video.video_loader import VideoLoader


@dataclass(slots=True)
class PlaybackStatus:
    """Live playback numbers handed to the frame sink on every draw."""

    frame_index: int
    frame_count: int
    elapsed: float
    target_fps: float
    actual_fps: float
    process_ms: float
    dropped: int


@dataclass(slots=True)
class PlaybackStats:
    """Summary returned when playback finishes."""

    frames_shown: int = 0
    frames_dropped: int = 0
    elapsed: float = 0.0
    target_fps: float = 0.0
    avg_process_ms: float = 0.0
    max_process_ms: float = 0.0
    completed: bool = False
    interrupted: bool = False

    @property
    def average_fps(self) -> float:
        """Frames actually displayed per second of wall-clock time."""
        return self.frames_shown / self.elapsed if self.elapsed > 0 else 0.0


class FrameSink(Protocol):
    """Anything that can display ASCII frames (implemented by the UI layer)."""

    def start(self) -> None: ...
    def draw(self, ascii_frame: str, status: PlaybackStatus) -> None: ...
    def stop(self) -> None: ...


class VideoPlayer:
    """Plays an opened :class:`VideoLoader` through an :class:`ASCIIConverter` into a sink.

    Timing is based on absolute deadlines (``start + n / fps``) so errors do not accumulate;
    when rendering falls behind, frames are skipped (if ``config.drop_frames``) to stay in sync.
    """

    def __init__(self, loader: VideoLoader, converter: ASCIIConverter, sink: FrameSink,
                 config: AppConfig, clock: Callable[[], float] = time.perf_counter,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._loader = loader
        self._converter = converter
        self._sink = sink
        self._config = config
        self._clock = clock
        self._sleep = sleep

    def play(self, stop_event: threading.Event | None = None) -> PlaybackStats:
        """Play until end of file, *stop_event* is set, or Ctrl+C.

        Always stops the sink (restoring the terminal). The loader is left open; the
        caller owns it.
        """
        info = self._loader.info
        fps = min(self._config.fps or info.fps, self._config.max_fps)
        interval = 1.0 / fps
        stats = PlaybackStats(target_fps=fps)
        total_process = 0.0
        index = 0
        start = self._clock()
        self._sink.start()
        try:
            while stop_event is None or not stop_event.is_set():
                deadline = start + index * interval
                if self._config.drop_frames:
                    late_frames = int((self._clock() - deadline) / interval)
                    if late_frames >= 1:
                        skipped = self._loader.skip(late_frames)
                        index += skipped
                        stats.frames_dropped += skipped
                        if skipped < late_frames:
                            stats.completed = True
                            break
                        deadline = start + index * interval
                ok, frame = self._loader.read()
                if not ok or frame is None:
                    stats.completed = True
                    break
                t0 = self._clock()
                text = self._converter.convert(frame)
                process = self._clock() - t0
                total_process += process
                stats.max_process_ms = max(stats.max_process_ms, process * 1000)
                wait = deadline - self._clock()
                if wait > 0:
                    self._sleep(wait)
                now = self._clock()
                stats.frames_shown += 1
                stats.elapsed = now - start
                self._sink.draw(text, PlaybackStatus(
                    frame_index=index + 1,
                    frame_count=info.frame_count,
                    elapsed=stats.elapsed,
                    target_fps=fps,
                    actual_fps=stats.average_fps,
                    process_ms=process * 1000,
                    dropped=stats.frames_dropped,
                ))
                index += 1
        except KeyboardInterrupt:
            stats.interrupted = True
        finally:
            stats.elapsed = self._clock() - start
            if stats.frames_shown:
                stats.avg_process_ms = total_process / stats.frames_shown * 1000
            self._sink.stop()
        return stats
