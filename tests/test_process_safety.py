"""Real-process safety: mpv must not outlive the app, even when the app is killed outright.

These tests start the real mpv (silently, volume 0) and are skipped when it is not installed.
"""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import textwrap
import time
import wave

import pytest

from src.audio.audio_player import find_mpv
from src.core.exceptions import MpvNotFoundError

try:
    MPV = find_mpv()
except MpvNotFoundError:
    MPV = None

needs_mpv = pytest.mark.skipif(MPV is None, reason="mpv is not installed")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows-only")


def make_wav(path, seconds=30):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(1000 * math.sin(2 * math.pi * 220 * i / 8000)))
                               for i in range(8000 * seconds)))


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def wait_gone(pid: int, timeout=5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


CHILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {root!r})
    from src.audio.playback_clock import PlaybackClock
    from src.audio.audio_player import MpvBackend
    clock = PlaybackClock()
    backend = MpvBackend(clock)
    backend.start({wav!r}, 0)
    backend.set_pause(False)
    print(backend._proc.pid, flush=True)
    {tail}
""")


def start_child(tmp_path, tail):
    from pathlib import Path

    wav = tmp_path / "tone.wav"
    make_wav(wav)
    root = str(Path(__file__).resolve().parents[1])
    child = subprocess.Popen([sys.executable, "-c", CHILD.format(root=root, wav=str(wav), tail=tail)],
                             stdout=subprocess.PIPE, text=True)
    mpv_pid = int(child.stdout.readline())
    return child, mpv_pid


@needs_mpv
@windows_only
def test_mpv_dies_when_the_app_is_force_killed(tmp_path):
    child, mpv_pid = start_child(tmp_path, "time.sleep(60)")
    try:
        assert pid_alive(mpv_pid)  # mpv is really running
        subprocess.run(["taskkill", "/F", "/PID", str(child.pid)], capture_output=True)  # like End task
        child.wait(timeout=10)
        assert wait_gone(mpv_pid), "mpv outlived a force-killed app"
    finally:
        subprocess.run(["taskkill", "/F", "/PID", str(mpv_pid)], capture_output=True)
        if child.poll() is None:
            child.kill()


@needs_mpv
def test_mpv_stops_on_normal_shutdown(tmp_path):
    child, mpv_pid = start_child(tmp_path, "time.sleep(0.5); backend.stop(); time.sleep(0.2)")
    try:
        child.wait(timeout=20)
        assert wait_gone(mpv_pid), "mpv outlived a normal stop()"
    finally:
        subprocess.run(["taskkill", "/F", "/PID", str(mpv_pid)], capture_output=True)


@needs_mpv
def test_mpv_stops_when_the_app_exits_without_calling_stop(tmp_path):
    child, mpv_pid = start_child(tmp_path, "time.sleep(0.5)  # exits here without stop()")
    try:
        child.wait(timeout=20)
        assert wait_gone(mpv_pid), "mpv outlived an app that exited without stop()"
    finally:
        subprocess.run(["taskkill", "/F", "/PID", str(mpv_pid)], capture_output=True)
