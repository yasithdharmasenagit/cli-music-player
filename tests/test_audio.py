from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from src.audio.audio_controller import MAX_VOLUME, AudioController
from src.audio.audio_player import MpvBackend, MpvIpcClient, find_mpv
from src.audio.playback_clock import PlaybackClock
from src.core.exceptions import AudioPlaybackError, MpvNotFoundError

# ------------------------------------------------------------------ clock


class FakeTime:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_clock_reports_mpv_position_and_extrapolates_bounded():
    ft = FakeTime()
    c = PlaybackClock(ft, max_extrapolation=0.25)
    assert c.position() == 0.0 and not c.started
    c.update(position=10.0, paused=False, duration=60)
    assert c.started and c.position() == 10.0
    ft.t += 0.1
    assert c.position() == pytest.approx(10.1)
    ft.t += 5  # no mpv update for a long time: do not run away
    assert c.position() == pytest.approx(10.25)
    c.update(position=10.4)  # re-anchors to mpv
    assert c.position() == pytest.approx(10.4)


def test_clock_pause_freezes_and_resume_continues():
    ft = FakeTime()
    c = PlaybackClock(ft)
    c.update(position=5.0, paused=False)
    ft.t += 0.2
    c.update(paused=True)
    frozen = c.position()
    assert frozen == pytest.approx(5.2)
    ft.t += 3
    assert c.position() == frozen and c.paused
    c.update(paused=False)
    ft.t += 0.1
    assert c.position() == pytest.approx(5.3)


def test_clock_seek_jump_duration_clamp_and_speed():
    ft = FakeTime()
    c = PlaybackClock(ft)
    c.update(position=30.0, paused=False, duration=31.0)
    c.update(position=0.0)  # seek backward is taken as-is
    assert c.position() == 0.0
    c.update(position=30.9)
    ft.t += 0.2
    assert c.position() == 31.0  # clamped to duration
    c2 = PlaybackClock(ft)
    c2.update(position=1.0, paused=False, speed=2.0)
    ft.t += 0.1
    assert c2.position() == pytest.approx(1.2)


def test_clock_end_and_error():
    c = PlaybackClock(FakeTime())
    c.update(position=1.0, paused=False)
    c.mark_ended()
    assert c.ended and c.error is None
    c2 = PlaybackClock(FakeTime())
    c2.set_error("boom")
    assert c2.ended and c2.error == "boom"


def test_clock_thread_safety_smoke():
    c = PlaybackClock()
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            c.update(position=i * 0.01, paused=False)
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    for _ in range(2000):
        assert c.position() >= 0
    stop.set()
    t.join()


# ------------------------------------------------------------------ IPC client


class FakeTransport:
    """Echoes a success reply for every command and lets tests inject events."""

    def __init__(self, reply=True, responses=None):
        self.incoming: queue.Queue[bytes] = queue.Queue()
        self.sent: list[dict] = []
        self.closed = False
        self.reply = reply
        self.responses = responses or {}

    def write(self, data: bytes) -> None:
        for raw in data.decode().splitlines():
            msg = json.loads(raw)
            self.sent.append(msg)
            if self.reply and "request_id" in msg:
                body = {"request_id": msg["request_id"], "error": "success", "data": None}
                body.update(self.responses.get(msg["command"][0], {}))
                self.incoming.put((json.dumps(body) + "\n").encode())

    def read(self, timeout: float) -> bytes:
        if self.closed:
            raise EOFError
        try:
            item = self.incoming.get(timeout=timeout)
        except queue.Empty:
            return b""
        if item is None:
            raise EOFError
        return item

    def push_event(self, **event):
        self.incoming.put((json.dumps(event) + "\n").encode())

    def close(self):
        self.closed = True


def make_client(transport, events=None, on_disconnect=None):
    events = events if events is not None else []
    client = MpvIpcClient(transport, events.append, on_disconnect, poll_interval=0.005)
    client.start()
    return client, events


def test_ipc_command_roundtrip_and_events():
    tr = FakeTransport(responses={"get_property": {"data": 42}})
    client, events = make_client(tr)
    assert client.command("get_property", "volume") == 42
    tr.push_event(event="property-change", name="time-pos", data=1.5)
    deadline = time.time() + 2
    while not events and time.time() < deadline:
        time.sleep(0.005)
    assert events[0]["data"] == 1.5
    client.close()
    assert tr.closed


def test_ipc_split_and_garbage_lines():
    tr = FakeTransport(reply=False)
    client, events = make_client(tr)
    tr.incoming.put(b"not json\n{\"event\":\"idle\"}\n{\"event\":")
    tr.incoming.put(b"\"pause\"}\n")
    deadline = time.time() + 2
    while len(events) < 2 and time.time() < deadline:
        time.sleep(0.005)
    assert [e["event"] for e in events] == ["idle", "pause"]
    client.close()


def test_ipc_error_reply_and_timeout():
    tr = FakeTransport(responses={"seek": {"error": "property unavailable"}})
    client, _ = make_client(tr)
    with pytest.raises(AudioPlaybackError, match="property unavailable"):
        client.command("seek", 5)
    client.close()
    silent = FakeTransport(reply=False)
    client2, _ = make_client(silent)
    with pytest.raises(AudioPlaybackError, match="did not answer"):
        client2.command("pause", timeout=0.1)
    client2.close()


def test_ipc_disconnect_wakes_waiters_and_notifies():
    tr = FakeTransport(reply=False)
    disconnected = threading.Event()
    client, _ = make_client(tr, on_disconnect=disconnected.set)
    result = {}

    def call():
        try:
            client.command("get_property", "x", timeout=3)
        except AudioPlaybackError as exc:
            result["err"] = str(exc)

    t = threading.Thread(target=call)
    t.start()
    time.sleep(0.05)
    tr.incoming.put(None)  # EOF
    t.join(2)
    assert disconnected.wait(2) and "disconnected" in result["err"]
    with pytest.raises(AudioPlaybackError):
        client.command("x")


# ------------------------------------------------------------------ backend (fake process)


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.terminated = self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def make_backend(clock, transport=None, proc=None):
    transport = transport or FakeTransport()
    proc = proc or FakeProc()
    launched = {}

    def popen(args, **kwargs):
        launched["args"] = args
        return proc

    backend = MpvBackend(clock, popen=popen, connect=lambda addr, alive: transport,
                         find=lambda configured: "mpv-fake")
    return backend, transport, proc, launched


def wait_for(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


def test_backend_launch_arguments_and_observers():
    clock = PlaybackClock()
    backend, tr, proc, launched = make_backend(clock)
    backend.start("song.mp3", 70, user_agent="UA")
    args = launched["args"]
    assert args[0] == "mpv-fake" and "--no-video" in args and "--pause=yes" in args
    assert "--volume=70" in args and "--ytdl=no" in args and "--user-agent=UA" in args
    assert args[-2:] == ["--", "song.mp3"]
    assert any(a.startswith("--input-ipc-server=") for a in args)
    observed = [m["command"][2] for m in tr.sent if m["command"][0] == "observe_property"]
    assert observed == ["time-pos", "pause", "duration", "volume", "speed"]
    backend.stop()
    assert tr.closed
    assert {"command": ["quit"]} in tr.sent


def test_backend_events_feed_clock():
    clock = PlaybackClock()
    backend, tr, _, _ = make_backend(clock)
    backend.start("x.mp3", 80)
    tr.push_event(event="property-change", id=3, name="duration", data=180.0)
    tr.push_event(event="property-change", id=2, name="pause", data=False)
    tr.push_event(event="property-change", id=1, name="time-pos", data=12.5)
    tr.push_event(event="property-change", id=1, name="time-pos", data=None)
    assert wait_for(lambda: clock.started)
    assert clock.duration == 180.0 and not clock.paused
    assert clock.position() >= 12.5
    tr.push_event(event="end-file", reason="eof")
    assert wait_for(lambda: clock.ended) and clock.error is None
    backend.stop()


def test_backend_playback_error_event():
    clock = PlaybackClock()
    backend, tr, _, _ = make_backend(clock)
    backend.start("x.mp3", 80)
    tr.push_event(event="end-file", reason="error", file_error="loading failed")
    assert wait_for(lambda: clock.error is not None)
    assert "loading failed" in clock.error
    backend.stop()


def test_backend_detects_unexpected_exit():
    clock = PlaybackClock()
    proc = FakeProc()
    backend, tr, _, _ = make_backend(clock, proc=proc)
    backend.start("x.mp3", 80)
    proc.returncode = 3
    tr.incoming.put(None)  # pipe closes because mpv died
    assert wait_for(lambda: clock.error is not None)
    assert "unexpectedly" in clock.error and "3" in clock.error
    backend.stop()


def test_backend_no_error_on_deliberate_stop():
    clock = PlaybackClock()
    backend, tr, _, _ = make_backend(clock)
    backend.start("x.mp3", 80)
    backend.stop()
    backend.stop()  # idempotent
    time.sleep(0.05)
    assert clock.error is None


def test_backend_connect_failure_cleans_up():
    proc = FakeProc()

    def bad_connect(addr, alive):
        raise AudioPlaybackError("nope")

    backend = MpvBackend(PlaybackClock(), popen=lambda a, **k: proc, connect=bad_connect,
                         find=lambda c: "mpv")
    with pytest.raises(AudioPlaybackError):
        backend.start("x", 80)


def test_backend_popen_oserror():
    def popen(args, **k):
        raise OSError(2, "No such file")

    backend = MpvBackend(PlaybackClock(), popen=popen, find=lambda c: "mpv")
    with pytest.raises(AudioPlaybackError, match="Could not start mpv"):
        backend.start("x", 80)


def test_backend_commands_require_running_engine():
    backend = MpvBackend(PlaybackClock(), find=lambda c: "mpv")
    with pytest.raises(AudioPlaybackError):
        backend.set_pause(True)


# ------------------------------------------------------------------ find_mpv


def test_find_mpv_paths(tmp_path):
    assert find_mpv(which=lambda n: "/bin/mpv" if n == "mpv" else None, env={}) == "/bin/mpv"
    exe = tmp_path / "mpv.exe"
    exe.write_text("x")
    assert find_mpv(str(exe), which=lambda n: None, env={}) == str(exe)
    assert find_mpv(None, which=lambda n: None, env={"MPV_PATH": str(exe)}) == str(exe)


def test_find_mpv_missing_gives_install_help(tmp_path):
    env = {"USERPROFILE": str(tmp_path), "LOCALAPPDATA": str(tmp_path), "ProgramFiles": str(tmp_path)}
    with pytest.raises(MpvNotFoundError, match="mpv.io"):
        find_mpv(None, which=lambda n: None, env=env)
    with pytest.raises(MpvNotFoundError, match="nowhere"):
        find_mpv("nowhere", which=lambda n: None, env=env)


# ------------------------------------------------------------------ controller


class RecordingBackend:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def start(self, source, volume, user_agent=None):
        self.calls.append(("start", source, volume, user_agent))

    def set_pause(self, paused):
        self._do(("pause", paused))

    def seek_relative(self, s):
        self._do(("seek", s))

    def seek_absolute(self, s):
        self._do(("seek_abs", s))

    def set_volume(self, v):
        self._do(("volume", v))

    def stop(self):
        self.calls.append(("stop",))

    def _do(self, call):
        if self.fail:
            raise AudioPlaybackError("mpv gone")
        self.calls.append(call)


def controller(backend=None, volume=80):
    backend = backend or RecordingBackend()
    ctl = AudioController(backend, PlaybackClock(), volume=volume, volume_step=5, seek_step=5)
    ctl.load("song.mp3", "UA")
    return ctl, backend


def test_controller_pause_toggle_resume():
    ctl, be = controller()
    assert ctl.paused  # loaded paused
    ctl.resume()
    assert not ctl.paused
    assert ctl.toggle_pause() is True and ctl.toggle_pause() is False
    assert [c for c in be.calls if c[0] == "pause"] == [
        ("pause", False), ("pause", True), ("pause", False)]


def test_controller_seek_and_volume():
    ctl, be = controller(volume=80)
    ctl.seek_forward()
    ctl.seek_backward()
    ctl.seek(-12.5)
    assert [c for c in be.calls if c[0] == "seek"] == [("seek", 5), ("seek", -5), ("seek", -12.5)]
    assert ctl.volume_up() == 85 and ctl.volume_down() == 80
    assert ctl.set_volume(500) == MAX_VOLUME and ctl.set_volume(-5) == 0
    assert ctl.volume_down() == 0


def test_controller_stop_idempotent_and_ignores_commands_after():
    ctl, be = controller()
    ctl.stop()
    ctl.stop()
    ctl.seek_forward()
    assert be.calls.count(("stop",)) == 1 and ("seek", 5) not in be.calls


def test_controller_backend_failure_sets_clock_error():
    ctl, _ = controller(RecordingBackend(fail=True))
    ctl.seek_forward()
    assert ctl.ended and "mpv gone" in ctl.error


def test_controller_exposes_clock_state():
    ctl, _ = controller()
    ctl.clock.update(position=3.0, duration=10.0, paused=False)
    assert ctl.position == pytest.approx(3.0, abs=0.3) and ctl.duration == 10.0


def test_controller_command_after_normal_end_is_not_an_error():
    ctl, _ = controller(RecordingBackend(fail=True))
    ctl.clock.mark_ended()
    ctl.seek_forward()
    assert ctl.error is None


def test_native_stderr_is_silenced_and_restored(tmp_path, capfd):
    import os

    from src.utils.stderr import suppress_native_stderr

    with suppress_native_stderr():
        os.write(2, b"ffmpeg noise\n")
    os.write(2, b"visible again\n")
    err = capfd.readouterr().err
    assert "ffmpeg noise" not in err and "visible again" in err
