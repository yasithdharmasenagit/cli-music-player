"""Shared fixtures: synthetic frames and videos (no downloads needed)."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest


@pytest.fixture
def make_video(tmp_path: Path):
    """Factory writing a small MJPG .avi with a moving bright square."""

    def _make(name: str = "clip.avi", frames: int = 10, size: tuple[int, int] = (64, 48),
              fps: float = 10.0) -> Path:
        path = tmp_path / name
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
        assert writer.isOpened()
        w, h = size
        for i in range(frames):
            img = np.zeros((h, w, 3), np.uint8)
            x = (i * 4) % (w - 10)
            img[10:20, x:x + 10] = (255, 255, 255)
            writer.write(img)
        writer.release()
        return path

    return _make
