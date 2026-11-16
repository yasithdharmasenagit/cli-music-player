# Release notes

## 1.0.0rc1 – release candidate

First release candidate: everything built in phases 1–3, optimized and documented. It is called a
*candidate* because the real-terminal, real-network checklist in
[docs/MANUAL_TESTING.md](docs/MANUAL_TESTING.md) has not been completed by a person yet (see
*Known limitations* in the README).

### Highlights

- Animated ASCII video from local files and YouTube, **real audio via mpv**, **automatic synchronized
  lyrics** (LRCLIB), keyboard controls, all inside one terminal screen.
- Video and lyrics follow **mpv's playback clock**; frames are dropped, never delayed, when the terminal
  cannot keep up, and audio runs in a separate process so it is never affected by drawing speed.
- Three quality levels that mean **picture detail**: **low** (chunky 2×2 blocks, cheapest), **medium** (default;
  every cell a sample, auto-contrast) and **high** (picture-first layout with far more characters on screen,
  70-level ramp, local contrast and sharpening). In a 120×40 window: 516 / 2,040 / 3,861 samples. Switch live with
  keys `1` / `2` / `3`.
- User **config file** (`config.toml`) with validation, `--init-config`, `--show-config`, plus a
  `--doctor` environment check.
- Rotating **log file** with automatic redaction of URL tokens, cookies and credentials, and
  per-session performance statistics.

### New in this release (since the phase 2 build)

**Performance** (measured; see [docs/BENCHMARKS.md](docs/BENCHMARKS.md))
- Character mapping works on bytes with reused buffers: per-frame Python allocations dropped by about
  93 % (71.6 → 5.4 KiB at 120 columns).
- The `low` profile uses a fast resize filter (about 100× cheaper for that step).
- The decoder thread decodes a few frames back-to-back into a bounded look-ahead buffer (≤ 8 frames and
  ≤ 32 MiB), then idles; the UI thread picks frames by the clock, so delivery stays exact. Measured: decode-thread
  CPU on 720p video 39 % → 25 % of a core, whole-session CPU about 18 % lower in alternating before/after runs
  (51 % vs 62 %); **no measurable change on 360p video**, and sync error 36 → 24 ms median.
- The UI loop waits for events instead of polling; idle CPU is lower (indicative: 1–5 % vs 5.7 %).
- Terminal size is polled a few times a second instead of every iteration.
- Memory stays flat across repeated sessions (leak check in the benchmark script).

**Interface**
- **Keys `1` / `2` / `3` switch the quality (low / medium / high) while playing**; the screen shows the current
  quality and a notice. `medium` is the same profile as `balanced` (both names work on the command line and in the
  config file). Custom `--width`, `--charset` and `--ui-fps` settings still apply after a switch.
- Animated loading card with a live checklist (media, video, audio engine, lyrics).
- Volume meter, coloured progress bar, transient notices (seek, stream reconnecting, lyrics service
  unavailable).

**Robustness**
- Dropped YouTube video streams are reopened at the current position with back-off; ffmpeg reconnect and
  timeout options for network sources, for mpv as well.
- mpv is killed on every exit path, including interpreter exit and a forced kill of the app (Windows job
  object; verified with a test using the real mpv that failed without it); a session can only run once;
  interactive mode keeps accepting tracks after errors and after each track ends.
- Native decoder warnings can no longer scribble over the screen.
- A damaged local video file is retried only a few times, then the picture simply stops.
- Fixed: **uneven motion ("shaking"/"jumping") on videos whose frame rate does not divide the redraw
  rate** (25, 30, 50, 60 fps vs the old fixed 24 redraws/s: 40 % of frame steps were uneven at 30 fps). The player
  now shows every k-th frame with a whole-number k, so spacing is constant (measured: 100 % identical steps at
  24/25/30/50/60 fps in every profile, and on a real YouTube stream); on a slow terminal k rises evenly.
- Fixed: **trembling motion from timing jitter.** Windows wakes waiting threads on a 15.6 ms grid, so each
  frame appeared 0–16 ms late at random (jitter 4.5 ms; p95 15 ms, 37 % of a 25 fps frame). The player now holds
  a 1 ms system timer during playback only: jitter 0.5 ms (max 2 ms), restored on every exit path.
- New: **`stabilize`** (on by default, `--no-stabilize` to disable). Video compression noise made still areas
  shimmer ("old TV"): on a real stream 14 % (`high`), 5 % (`balanced`) and 2 % (`low`) of near-static characters
  flipped every frame. A character now changes only when its brightness has moved by about one ramp step:
  flicker in still areas dropped to 1.6 %, 1.0 % and 0.2 %, while real motion, fades and scene cuts still show.
- Fixed: a backward seek (←) could be ignored by the video after look-ahead decoding was added
  (caught by the new tests before release).
- Fixed: a custom character ramp lost its leading space (the darkest character) when read from the config.

**Operations**
- Platform-appropriate default folders (`%APPDATA%` for config, `%LOCALAPPDATA%` for cache and logs).
- `--version`, `requirements.txt`, a pinned `requirements-lock.txt`, `config.example.toml`.
- Optional PyInstaller build script (`scripts/build_exe.ps1`); the resulting one-folder build was smoke-tested
  (version, doctor, silent playback, real mpv audio, lyrics, YouTube resolution, force-kill cleanup).

### Changes to be aware of

- **Default quality is now `medium`** (= `balanced`; 24-level ramp, every cell a sample). Earlier builds defaulted
  to the 70-level ramp; use `-q high` or `quality = "high"` (now also a more spacious picture-first layout) to get
  more detail. `--quality standard` and `medium` are accepted as aliases of `balanced`.
- **Quality used to change almost nothing visible** (medium and high drew the same 85×24 grid in a 120×40
  window, differing only in the character ramp). The levels now differ in resolution, layout and enhancement.
- The `--log` option was replaced: logging is on by default; use `--no-log` to disable and
  `--log-level` to change verbosity. Logs and the lyrics cache moved from `data/cache` to
  `%LOCALAPPDATA%\ascii-music-player\cache` (override with `--cache-dir` / `[paths] cache_dir`).
- Pillow is no longer a dependency (it was never used).
- Default redraw rate is quality-dependent (15/30/30 per second instead of one fixed 24).

### Testing

- `python -m pytest` – 385 tests (no network, mpv or YouTube required) and `python -m mypy` (strict).
- The install instructions were verified from scratch in a clean virtual environment: install,
  tests, `--doctor`, the `ascii-music-player` command.
- Real mpv was exercised against audio files and the real Windows named-pipe transport; real YouTube
  resolution and streaming, and the real LRCLIB API, were exercised in targeted checks.

### Upgrade

```powershell
git pull
pip install -e .          # or: pip install -r requirements-lock.txt ; pip install -e . --no-deps
python run.py --doctor
```

## Earlier phases

- **Phase 2** – mpv audio engine and IPC, shared playback clock, YouTube and local media resolution,
  LRCLIB lyrics with LRC parsing/sync/cache, combined Rich screen, keyboard controls.
- **Phase 1** – ASCII rendering engine: video loader, frame processor, ASCII converter, timed
  silent player, Rich terminal interface.
