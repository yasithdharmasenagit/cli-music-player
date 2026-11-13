"""mpv process management and JSON IPC client.

mpv is started headless (``--no-video``) with an IPC server (named pipe on Windows, Unix
socket elsewhere). A single I/O thread owns the connection: it writes queued commands and
polls for incoming data, which avoids the deadlock that a blocking read on one thread and a
write on another would cause with a synchronous Windows pipe handle.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from src.audio.playback_clock import PlaybackClock
from src.core.exceptions import AudioPlaybackError, MpvNotFoundError
from src.utils.logger import get_logger

_log = get_logger("audio")

MPV_INSTALL_HELP = (
    "mpv was not found. Install it and make sure it is on your PATH:\n"
    "  winget install shinchiro.mpv     (or: scoop install mpv)\n"
    "  or download from https://mpv.io/installation/\n"
    "Then restart the terminal. You can also point to it with --mpv-path."
)

# Property observer ids -> names
_OBSERVED = {1: "time-pos", 2: "pause", 3: "duration", 4: "volume", 5: "speed"}


def find_mpv(configured: str | None = None, which: Callable[[str], str | None] = shutil.which,
             env: dict[str, str] | None = None) -> str:
    """Locate the mpv executable.

    Raises:
        MpvNotFoundError: with installation instructions.
    """
    env = dict(os.environ) if env is None else env
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    if env.get("MPV_PATH"):
        candidates.append(env["MPV_PATH"])
    for cand in candidates:
        found = which(cand) or (cand if Path(cand).is_file() else None)
        if found:
            return str(found)
    found = which("mpv") or which("mpv.exe")
    if found:
        return found
    if sys.platform == "win32":
        home = Path(env.get("USERPROFILE", str(Path.home())))
        local = Path(env.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        for loc in (home / "scoop" / "apps" / "mpv" / "current" / "mpv.exe",
                    local / "Programs" / "mpv" / "mpv.exe",
                    Path(env.get("ProgramFiles", r"C:\Program Files")) / "mpv" / "mpv.exe",
                    Path(env.get("ProgramFiles", r"C:\Program Files")) / "MPV Player" / "mpv.exe",
                    Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "mpv" / "mpv.exe"):
            if loc.is_file():
                return str(loc)
    if configured:
        raise MpvNotFoundError(f"mpv not found at '{configured}'.\n{MPV_INSTALL_HELP}")
    raise MpvNotFoundError(MPV_INSTALL_HELP)


# --------------------------------------------------------------------------- transports


class Transport(Protocol):
    """Byte pipe to mpv. All methods are called only from the IPC thread (except close)."""

    def write(self, data: bytes) -> None: ...
    def read(self, timeout: float) -> bytes:
        """Return available bytes, ``b''`` if none within *timeout*; raise EOFError if closed."""
        ...
    def close(self) -> None: ...


class WindowsPipeTransport:
    """Named-pipe transport using PeekNamedPipe so reads never block writes."""

    def __init__(self, path: str) -> None:
        self._file = open(path, "r+b", buffering=0)  # noqa: SIM115 - closed in close()
        import msvcrt
        self._fd = self._file.fileno()
        self._handle = msvcrt.get_osfhandle(self._fd)  # type: ignore[attr-defined]

    def write(self, data: bytes) -> None:
        self._file.write(data)

    def read(self, timeout: float) -> bytes:
        import ctypes
        from ctypes import wintypes
        peek = ctypes.windll.kernel32.PeekNamedPipe  # type: ignore[attr-defined]
        avail = wintypes.DWORD(0)
        deadline = time.monotonic() + timeout
        while True:
            ok = peek(wintypes.HANDLE(self._handle), None, 0, None, ctypes.byref(avail), None)
            if not ok:
                raise EOFError("mpv pipe closed")
            if avail.value:
                return os.read(self._fd, min(avail.value, 65536))
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.003)

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:
            pass


class UnixSocketTransport:
    """Unix-domain-socket transport (Linux/macOS)."""

    def __init__(self, path: str) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
        self._sock.connect(path)

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    def read(self, timeout: float) -> bytes:
        self._sock.settimeout(timeout)
        try:
            data = self._sock.recv(65536)
        except socket.timeout:
            return b""
        if not data:
            raise EOFError("mpv socket closed")
        return data

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def ipc_address() -> str:
    """A unique IPC endpoint name for this process."""
    token = f"ascii-music-player-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if sys.platform == "win32":
        return rf"\\.\pipe\{token}"
    return str(Path(tempfile.gettempdir()) / f"{token}.sock")


def connect_transport(address: str, alive: Callable[[], bool], timeout: float = 8.0) -> Transport:
    """Connect to mpv's IPC endpoint, retrying while the process starts up."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        if not alive():
            raise AudioPlaybackError("mpv exited before it could be controlled.")
        try:
            if sys.platform == "win32":
                return WindowsPipeTransport(address)
            return UnixSocketTransport(address)
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    raise AudioPlaybackError(f"Could not connect to mpv ({last}).")


# --------------------------------------------------------------------------- IPC client


class MpvIpcClient:
    """JSON IPC client. Commands are synchronous; events go to *on_event* on the IPC thread."""

    def __init__(self, transport: Transport, on_event: Callable[[dict[str, Any]], None],
                 on_disconnect: Callable[[], None] | None = None,
                 poll_interval: float = 0.005) -> None:
        self._transport = transport
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        self._poll = poll_interval
        self._outgoing: queue.Queue[bytes] = queue.Queue()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mpv-ipc", daemon=True)
        self.connected = True

    def start(self) -> None:
        """Start the I/O thread."""
        self._thread.start()

    def command(self, *args: Any, timeout: float = 2.0) -> Any:
        """Send a command and wait for mpv's reply.

        Raises:
            AudioPlaybackError: if disconnected, mpv reports an error, or the reply times out.
        """
        if not self.connected:
            raise AudioPlaybackError("mpv is not running.")
        request_id = next(self._ids)
        done = threading.Event()
        slot: dict[str, Any] = {}
        with self._lock:
            self._pending[request_id] = (done, slot)
        payload = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
        self._outgoing.put(payload.encode("utf-8"))
        if not done.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise AudioPlaybackError(f"mpv did not answer command '{args[0]}'.")
        if slot.get("error") not in (None, "success"):
            raise AudioPlaybackError(f"mpv command '{args[0]}' failed: {slot['error']}")
        return slot.get("data")

    def send_nowait(self, *args: Any) -> None:
        """Fire-and-forget command (used during shutdown)."""
        self._outgoing.put((json.dumps({"command": list(args)}) + "\n").encode("utf-8"))

    def close(self) -> None:
        """Stop the I/O thread and close the transport."""
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)
        self._transport.close()
        self.connected = False

    def _run(self) -> None:
        buffer = b""
        try:
            while not self._stop.is_set():
                try:
                    while True:
                        self._transport.write(self._outgoing.get_nowait())
                except queue.Empty:
                    pass
                chunk = self._transport.read(self._poll)
                if not chunk:
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._handle_line(line)
        except (EOFError, OSError, ValueError):
            pass
        finally:
            self.connected = False
            with self._lock:  # wake any waiters
                for done, slot in self._pending.values():
                    slot.setdefault("error", "disconnected")
                    done.set()
                self._pending.clear()
            if not self._stop.is_set() and self._on_disconnect is not None:
                self._on_disconnect()

    def _handle_line(self, line: bytes) -> None:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if "request_id" in msg:
            with self._lock:
                entry = self._pending.pop(int(msg["request_id"]), None)
            if entry is not None:
                entry[1].update(msg)
                entry[0].set()
        elif "event" in msg:
            try:
                self._on_event(msg)
            except Exception:  # an event handler bug must never kill the IPC thread
                _log.exception("Error in mpv event handler")


# --------------------------------------------------------------------------- backend


class AudioBackend(Protocol):
    """What :class:`~src.audio.audio_controller.AudioController` needs from an audio engine."""

    def start(self, source: str, volume: int, user_agent: str | None = None) -> None: ...
    def set_pause(self, paused: bool) -> None: ...
    def seek_relative(self, seconds: float) -> None: ...
    def seek_absolute(self, seconds: float) -> None: ...
    def set_volume(self, volume: int) -> None: ...
    def stop(self) -> None: ...


class MpvBackend:
    """Audio engine backed by an mpv subprocess.

    Starts paused, so the caller can prepare video and then :meth:`set_pause` ``(False)`` to
    start audio and video together.
    """

    def __init__(self, clock: PlaybackClock, mpv_path: str | None = None,
                 popen: Callable[..., Any] = subprocess.Popen,
                 connect: Callable[[str, Callable[[], bool]], Transport] = connect_transport,
                 find: Callable[[str | None], str] = find_mpv) -> None:
        self._clock = clock
        self._configured = mpv_path
        self._popen = popen
        self._connect = connect
        self._find = find
        self._proc: Any | None = None
        self._ipc: MpvIpcClient | None = None
        self._closing = False

    # -- lifecycle

    def start(self, source: str, volume: int, user_agent: str | None = None) -> None:
        """Launch mpv paused on *source* and begin observing playback properties.

        Raises:
            MpvNotFoundError, AudioPlaybackError
        """
        exe = self._find(self._configured)
        address = ipc_address()
        args = [exe, "--no-video", "--no-terminal", "--really-quiet", "--no-config",
                "--force-window=no", "--audio-display=no", "--ytdl=no", "--idle=no",
                "--pause=yes", f"--volume={volume}", f"--input-ipc-server={address}",
                "--input-default-bindings=no"]
        if user_agent:
            args.append(f"--user-agent={user_agent}")
        args += ["--", source]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self._proc = self._popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=flags)
        except OSError as exc:
            raise AudioPlaybackError(f"Could not start mpv: {exc.strerror or exc}") from exc
        proc = self._proc
        try:
            transport = self._connect(address, lambda: proc.poll() is None)
            self._ipc = MpvIpcClient(transport, self._on_event, self._on_disconnect)
            self._ipc.start()
            for obs_id, name in _OBSERVED.items():
                self._ipc.command("observe_property", obs_id, name)
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        """Quit mpv and release every resource. Safe to call repeatedly."""
        self._closing = True
        ipc, self._ipc = self._ipc, None
        proc, self._proc = self._proc, None
        if ipc is not None:
            if ipc.connected:
                ipc.send_nowait("quit")
                time.sleep(0.05)  # give the I/O thread a chance to flush the quit command
            ipc.close()
        if proc is not None:
            try:
                proc.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        proc.kill()
                    except OSError:
                        pass

    # -- commands

    def _cmd(self, *args: Any) -> Any:
        if self._ipc is None:
            raise AudioPlaybackError("Audio engine is not running.")
        return self._ipc.command(*args)

    def set_pause(self, paused: bool) -> None:
        self._cmd("set_property", "pause", paused)

    def seek_relative(self, seconds: float) -> None:
        self._cmd("seek", seconds, "relative")

    def seek_absolute(self, seconds: float) -> None:
        self._cmd("seek", seconds, "absolute")

    def set_volume(self, volume: int) -> None:
        self._cmd("set_property", "volume", volume)

    # -- events (IPC thread)

    def _on_event(self, msg: dict[str, Any]) -> None:
        event = msg.get("event")
        if event == "property-change":
            name, data = msg.get("name"), msg.get("data")
            if data is None:
                return
            if name == "time-pos":
                self._clock.update(position=float(data))
            elif name == "pause":
                self._clock.update(paused=bool(data))
            elif name == "duration":
                self._clock.update(duration=float(data))
            elif name == "speed":
                self._clock.update(speed=float(data))
        elif event == "end-file":
            reason = msg.get("reason")
            if reason == "error":
                detail = msg.get("file_error") or "unknown error"
                self._clock.set_error(f"Audio playback failed: {detail}")
            elif reason in ("eof", "stop", "quit"):
                self._clock.mark_ended()

    def _on_disconnect(self) -> None:
        if self._closing or self._clock.ended:
            return
        code = self._proc.poll() if self._proc is not None else None
        self._clock.set_error(f"mpv stopped unexpectedly (exit code {code}).")
