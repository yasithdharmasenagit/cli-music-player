"""Reproducible performance benchmark for the ASCII rendering pipeline.

Usage (from the repository root):

    python scripts/benchmark.py                    # stage timings at several widths
    python scripts/benchmark.py --e2e              # + headless full-session CPU / sync run
    python scripts/benchmark.py --json out.json    # machine-readable results

Test media is generated locally with a fixed random seed (no downloads), so runs are
comparable on one machine. Numbers depend on the machine: only compare runs made on the same one.
"""

from __future__ import annotations

import argparse
import gc
import io
import json
import os
import platform
import statistics
import sys
import threading
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from rich.console import Console  # noqa: E402

from src.core.config import AppConfig, Quality  # noqa: E402
from src.ui.player_view import ViewState, build_player_view, video_area  # noqa: E402
from src.video.ascii_converter import ASCIIConverter  # noqa: E402
from src.video.video_loader import VideoLoader  # noqa: E402

BENCH_DIR = ROOT / "data" / "cache" / "bench"
WIDTHS = (60, 80, 120, 160, 200)


def make_converter(**kwargs) -> ASCIIConverter:
    """Build a converter, dropping options an older code base does not know (for comparisons)."""
    import inspect
    accepted = inspect.signature(ASCIIConverter.__init__).parameters
    return ASCIIConverter(**{k: v for k, v in kwargs.items() if k in accepted})


def make_media(width: int, height: int, fps: int = 30, seconds: int = 12) -> Path:
    """Deterministic synthetic clip (moving gradients + noise so MJPG cannot cheat)."""
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    path = BENCH_DIR / f"bench_{width}x{height}_{fps}fps_{seconds}s.avi"
    if path.exists() and path.stat().st_size > 0:
        return path
    rng = np.random.default_rng(1234)
    partial = path.with_suffix(".partial.avi")  # renamed only when complete
    writer = cv2.VideoWriter(str(partial), cv2.VideoWriter_fourcc(*"MJPG"), fps, (width, height))
    yy, xx = np.mgrid[0:height, 0:width]
    for i in range(fps * seconds):
        img = np.empty((height, width, 3), np.uint8)
        img[..., 0] = (128 + 127 * np.sin(xx / 53.0 + i / 7.0)).astype(np.uint8)
        img[..., 1] = (128 + 127 * np.sin(yy / 41.0 - i / 11.0)).astype(np.uint8)
        img[..., 2] = ((xx + yy + i * 5) % 256).astype(np.uint8)
        noise = rng.integers(0, 40, (height, width, 3), dtype=np.uint8)
        img = cv2.add(img, noise)
        cv2.circle(img, (int(width / 2 + width / 3 * np.cos(i / 9)), int(height / 2)), height // 8,
                   (255, 255, 255), -1)
        writer.write(img)
    writer.release()
    partial.replace(path)
    return path


def load_frames(path: Path, count: int) -> list[np.ndarray]:
    frames: list[np.ndarray] = []
    with VideoLoader(path) as loader:
        while len(frames) < count:
            ok, frame = loader.read()
            if not ok or frame is None:
                break
            frames.append(frame.copy())
    return frames


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def summarize(ms: list[float]) -> dict[str, float]:
    return {"median": statistics.median(ms), "p95": pct(ms, 0.95), "max": max(ms)}


def time_each(fn, items, repeat: int = 1) -> list[float]:
    out: list[float] = []
    for _ in range(repeat):
        for item in items:
            t0 = time.perf_counter_ns()
            fn(item)
            out.append((time.perf_counter_ns() - t0) / 1e6)
    return out


def bench_decode(path: Path, count: int = 150) -> dict[str, float]:
    times: list[float] = []
    with VideoLoader(path) as loader:
        for _ in range(count):
            t0 = time.perf_counter_ns()
            ok, _ = loader.read()
            times.append((time.perf_counter_ns() - t0) / 1e6)
            if not ok:
                break
    return summarize(times)


def bench_stages(frames: list[np.ndarray], width: int, charset: str, color: bool,
                 interpolation: str = "area") -> dict:
    height_rows = 50
    conv = make_converter(width=width, charset=charset, color=color, max_rows=height_rows,
                          interpolation=interpolation)
    for f in frames[:10]:  # warm-up
        conv.convert(f)
    process_ms = time_each(conv.processor.process, frames)
    processed = [conv.processor.process(f) for f in frames]
    map_ms = time_each(conv.render, processed)
    text = conv.convert(frames[0])
    cols = len(text.split("\n")[0]) if not color else width
    rows = text.count("\n") + 1
    # Terminal rendering: Rich layout build + write to an in-memory terminal of the right size.
    console = Console(file=io.StringIO(), force_terminal=True, width=cols + 2, height=rows + 14,
                      color_system="truecolor")
    state = ViewState(title="Benchmark Track", artist="Synthetic", origin="Local file", position=42,
                      duration=180, paused=False, volume=80, color=color, ascii_frame=text)
    layout_ms: list[float] = []
    for _ in range(60):
        t0 = time.perf_counter_ns()
        console.print(build_player_view(state, cols + 2))
        layout_ms.append((time.perf_counter_ns() - t0) / 1e6)
        console.file.seek(0)
        console.file.truncate()
    total_ms = [a + b for a, b in zip(process_ms, map_ms)]
    out_bytes = len(console.file.getvalue())
    return {"cols": cols, "rows": rows, "resize_gray": summarize(process_ms),
            "char_map": summarize(map_ms), "ascii_total": summarize(total_ms),
            "terminal_render": summarize(layout_ms[5:]), "chars_per_frame": len(text)}


def alloc_profile(frames: list[np.ndarray], width: int, charset: str,
                  interpolation: str = "area") -> dict[str, float]:
    conv = make_converter(width=width, charset=charset, max_rows=50, interpolation=interpolation)
    for f in frames[:5]:
        conv.convert(f)
    tracemalloc.start()
    tracemalloc.reset_peak()
    base = tracemalloc.get_traced_memory()[0]
    for f in frames[:100]:
        conv.convert(f)
    peak = tracemalloc.get_traced_memory()[1] - base
    tracemalloc.stop()
    return {"peak_kib_per_conversion": peak / 1024.0}


def rss_mib() -> float | None:
    """Resident memory of this process (Windows via psapi, POSIX via /proc)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = PMC()
        counters.cb = ctypes.sizeof(PMC)
        handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
        ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
            ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb)
        return counters.WorkingSetSize / 2**20
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        return None
    return None


# ----------------------------------------------------------------------------- end to end


class NullTerminal(io.TextIOBase):
    """Discards output (and claims to be a terminal) so the benchmark's own memory stays flat."""

    def write(self, s: str) -> int:
        return len(s)

    def isatty(self) -> bool:
        return True


class ClockDriver:
    """Stand-in for mpv: feeds the shared PlaybackClock in real time (like mpv's IPC events)."""

    def __init__(self, clock, duration: float):
        self.clock, self.duration = clock, duration
        self._pos, self._paused, self._stop = 0.0, True, threading.Event()

    def start(self, source, volume, user_agent=None):
        self.clock.update(position=0.0, paused=True, duration=self.duration)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        last = time.monotonic()
        while not self._stop.is_set():
            time.sleep(0.02)  # mpv reports roughly every 20-70 ms
            now = time.monotonic()
            if not self._paused:
                self._pos += now - last
                if self._pos >= self.duration:
                    self.clock.mark_ended()
                    return
                self.clock.update(position=self._pos)
            last = now

    def set_pause(self, paused):
        self._paused = paused
        self.clock.update(paused=paused)

    def seek_relative(self, s): ...
    def seek_absolute(self, s): ...
    def set_volume(self, v): ...

    def stop(self):
        self._stop.set()


def bench_e2e(path: Path, config: AppConfig, seconds: float, console_size: tuple[int, int]) -> dict:
    from src.audio.audio_controller import AudioController
    from src.audio.playback_clock import PlaybackClock
    from src.media.media_resolver import MediaResolver
    from src.player.session import PlaybackSession
    from src.ui.keyboard import KeyReader

    media = MediaResolver(probe=lambda p: None).resolve_local(path)
    clock = PlaybackClock()
    audio = AudioController(ClockDriver(clock, seconds), clock)
    console = Console(file=NullTerminal(), force_terminal=True, width=console_size[0],
                      height=console_size[1], color_system="truecolor")
    session = PlaybackSession(media, config, audio, None, console, KeyReader(None))

    errors: list[float] = []
    stop = threading.Event()

    def sampler():  # |picture time - audio clock time| sampled every 50 ms
        while not stop.is_set():
            time.sleep(0.05)
            src, bg = session._source, session._bg
            # The picture on screen is the last frame *delivered* to the UI (decoding runs ahead).
            shown = getattr(bg, "last_delivered", None)
            if shown is None and src is not None:  # older code base: no look-ahead, so decoded == shown
                shown = src.shown_index
            if src is not None and bg is not None and clock.started and not clock.paused                     and shown is not None and shown >= 0:
                errors.append(abs(clock.position() - shown / src.fps) * 1000)

    threading.Thread(target=sampler, daemon=True).start()
    cpu0, wall0 = time.process_time(), time.perf_counter()
    rss0 = rss_mib()
    result = session.run()
    stop.set()
    cpu, wall = time.process_time() - cpu0, time.perf_counter() - wall0
    return {
        "wall_s": wall, "cpu_percent_of_one_core": 100.0 * cpu / wall,
        "frames_decoded": result.frames_decoded, "frames_skipped": result.frames_skipped,
        "seeks": result.seeks, "stalls": result.stalls, "max_render_ms": result.max_render_ms,
        "sync_error_ms_median": statistics.median(errors) if errors else None,
        "sync_error_ms_p95": pct(errors, 0.95) if errors else None,
        "rss_mib_before": rss0, "rss_mib_after": rss_mib(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--resolution", default="1280x720", help="source video size (default 1280x720)")
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--e2e", action="store_true", help="also run a headless full-session test")
    ap.add_argument("--e2e-seconds", type=float, default=8.0)
    ap.add_argument("--no-stages", action="store_true", help="skip the per-stage timing tables")
    ap.add_argument("--sizes", default="100x30,160x45,220x60",
                    help="terminal sizes for the e2e runs, e.g. 160x45 (default: three sizes)")
    ap.add_argument("--width", type=int, default=None,
                    help="fix the ASCII width for e2e runs (use the same value when comparing versions)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="repeat each e2e run N times and report median and range (CPU is noisy)")
    ap.add_argument("--leak", type=int, default=0, metavar="N",
                    help="run N consecutive sessions and report memory / thread growth")
    ap.add_argument("--color", action="store_true", help="benchmark colour mode too")
    ap.add_argument("--json", type=Path, help="write results as JSON")
    ap.add_argument("--quality", choices=[q.name.lower() for q in Quality], default=None,
                    help="only this quality profile (default: all profiles)")
    args = ap.parse_args()

    w, h = (int(v) for v in args.resolution.lower().split("x"))
    path = make_media(w, h)
    frames = load_frames(path, args.frames)
    results: dict = {"env": {
        "python": platform.python_version(), "platform": platform.platform(),
        "cpu_count": os.cpu_count(), "opencv": cv2.__version__, "numpy": np.__version__,
        "source": f"{w}x{h} MJPG synthetic, {len(frames)} frames"}, "decode": bench_decode(path),
        "stages": {}, "e2e": {}}
    print(f"Source: {w}x{h} synthetic MJPG | Python {platform.python_version()} | "
          f"OpenCV {cv2.__version__} | NumPy {np.__version__} | {os.cpu_count()} logical CPUs")
    d = results["decode"]
    print(f"Decode (MJPG, {w}x{h}): median {d['median']:.2f} ms, p95 {d['p95']:.2f} ms\n")

    profiles = [Quality[args.quality.upper()]] if args.quality else list(Quality)
    for quality in ([] if args.no_stages else profiles):
        charset = getattr(quality, "charset", None) or quality.value
        for color in ([False, True] if args.color else [False]):
            print(f"== quality={quality.name} ({len(charset)}-char ramp) color={color}")
            print(f"{'width':>5} {'grid':>9} {'resize+gray':>12} {'char map':>9} {'ASCII total':>12} "
                  f"{'terminal':>9} {'p95 all':>8}  (median ms)")
            for width in WIDTHS:
                interp = getattr(getattr(quality, "profile", None), "interpolation", "area")
                r = bench_stages(frames, width, charset, color, interp)
                key = f"{quality.name}/{'color' if color else 'mono'}/{width}"
                r["alloc"] = alloc_profile(frames, width, charset, interp)
                results["stages"][key] = r
                total_p95 = r["ascii_total"]["p95"] + r["terminal_render"]["p95"]
                print(f"{width:>5} {r['cols']:>4}x{r['rows']:<4} {r['resize_gray']['median']:>12.2f} "
                      f"{r['char_map']['median']:>9.2f} {r['ascii_total']['median']:>12.2f} "
                      f"{r['terminal_render']['median']:>9.2f} {total_p95:>8.2f}")
            print()

    if args.e2e:
        del frames  # the stage benchmark holds raw frames; free them so RSS reflects the player
        gc.collect()
        for quality in profiles:
            cfg = AppConfig(quality=quality, alt_screen=False,
                            **({"ascii_width": args.width} if args.width else {}))
            for cols, rows in [tuple(int(v) for v in s.split("x")) for s in args.sizes.split(",")]:
                runs = [bench_e2e(path, cfg, args.e2e_seconds, (cols, rows)) for _ in range(args.repeat)]
                r = dict(runs[-1])
                cpus = sorted(x["cpu_percent_of_one_core"] for x in runs)
                r["cpu_percent_of_one_core"] = statistics.median(cpus)
                r["cpu_range"] = [cpus[0], cpus[-1]]
                results["e2e"][f"{quality.name}/{cols}x{rows}"] = r
                spread = f" (range {cpus[0]:.0f}-{cpus[-1]:.0f}, n={len(cpus)})" if len(cpus) > 1 else ""
                print(f"e2e {quality.name:<8} terminal {cols}x{rows}: CPU {r['cpu_percent_of_one_core']:.0f}%{spread} of one core, "
                      f"decoded {r['frames_decoded']}, skipped {r['frames_skipped']}, seeks {r['seeks']}, "
                      f"stalls {r['stalls']}, sync error median {r['sync_error_ms_median']:.0f} ms / "
                      f"p95 {r['sync_error_ms_p95']:.0f} ms, RSS {r['rss_mib_before']:.0f}->"
                      f"{r['rss_mib_after']:.0f} MiB")
    if args.leak:
        cfg = AppConfig(quality=Quality.BALANCED, alt_screen=False)
        base_threads = threading.active_count()
        rss: list[float] = []
        for i in range(args.leak):
            bench_e2e(path, cfg, 4.0, (160, 45))
            gc.collect()
            value = rss_mib() or 0.0
            rss.append(value)
            print(f"leak check: session {i + 1:>2}/{args.leak}: RSS {value:.1f} MiB, "
                  f"threads {threading.active_count()} (baseline {base_threads})")
        half = len(rss) // 2
        growth = (statistics.mean(rss[half:]) - statistics.mean(rss[:half])) if half else 0.0
        per_session = (rss[-1] - rss[half]) / max(1, len(rss) - 1 - half) if half else 0.0
        results["leak"] = {"rss_mib": rss, "growth_between_halves_mib": growth,
                           "growth_per_session_second_half_mib": per_session,
                           "threads_after": threading.active_count(), "threads_before": base_threads}
        print(f"leak check: mean RSS first half {statistics.mean(rss[:half]):.1f} MiB -> second half "
              f"{statistics.mean(rss[half:]):.1f} MiB; growth/session in second half {per_session:+.2f} MiB; "
              f"threads {base_threads} -> {threading.active_count()}")
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
