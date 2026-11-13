"""Silence native (C-library) stderr output while the full-screen player is running."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def suppress_native_stderr() -> Iterator[None]:
    """Redirect file descriptor 2 to the null device.

    FFmpeg/OpenCV write decoder warnings (``Error splitting the input into NAL units`` ...)
    directly to fd 2, bypassing Python. Printed over a Rich Live screen they corrupt the
    picture. The descriptor is always restored, so errors shown afterwards are visible.
    """
    saved: int | None = None
    devnull: int | None = None
    try:
        saved = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
    except OSError:  # no usable stderr (e.g. closed); nothing to suppress
        if saved is not None:
            os.close(saved)
        saved = None
    try:
        yield
    finally:
        if saved is not None:
            os.dup2(saved, 2)
            os.close(saved)
        if devnull is not None:
            os.close(devnull)
