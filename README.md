# ASCII Music Player

Watch music videos as animated ASCII art in your terminal, with the real audio and automatically
fetched, time-synchronized lyrics. Paste a YouTube link or point it at a local file; everything runs
inside PowerShell / Windows Terminal.

```
╭──────────────────────────────────────────────────────────────────────────────────╮
│ ASCII MUSIC PLAYER                                                               │
│ NOW PLAYING  Example Song  —  Example Artist   [Local file]                      │
╰──────────────────────────────────────────────────────────────────────────────────╯
╭──────────────────────────────────────────────────────────────────────────────────╮
│                       `,Ii~----~~~ii~~-[1|||||11111|ttruzz                       │
│                            ````      ``,Iii~iiiIIIIi~-[[11                       │
│                       ,IIi~-----~~~~~-[1||ttt||111||truzzz                       │
│                       truzULLLLUUUzzUULOwdddddwwwwwdhh*W%%                       │
│                       uuzUd$$$%wLLUULLOwdhhhhhdddddh*WW%$$                       │
│                       ii~-%$$$$d[[[[[1||trrurrtttttruzUULL                       │
╰──────────────────────────────────────────────────────────────────────────────────╯
╭─ LYRICS ─────────────────────────────────────────────────────────────────────────╮
│                               Previous lyric line                                │
│                        Current lyric line is highlighted                         │
│                                 Next lyric line                                  │
╰──────────────────────────────────────────────────────────────────────────────────╯
╭──────────────────────────────────────────────────────────────────────────────────╮
│ 01:24 / 04:12   ▶ Playing                                                        │
│ [━━━━━━━━━━━━━━━━━━━━━━━━━━────────────────────────────────────────────────────] │
│ Volume ████████░░ 80%   SPACE pause · ←/→ seek · ↑/↓ volume · 1/2/3 quality · L… │
```

*A text rendering of the real interface (cropped), produced by `python scripts/demo_capture.py` from
the bundled synthetic clip and example lyric lines. It is not a screenshot of a live session; in the
terminal the picture is animated and the cyan/blue accents are coloured.*

## Features

- **ASCII video** from local video files and YouTube, with three quality profiles
  (low / balanced / high), optional colour, brightness inversion and custom character ramps.
- **Real audio** through [mpv](https://mpv.io). mpv's playback clock is the single timeline: the
  picture and the lyrics are chosen *from the audio position*, never from independent timers.
- **Automatic synchronized lyrics** from [LRCLIB](https://lrclib.net) – highlighted current line with
  the previous and next lines around it, cached on disk. Plain (un-timed) lyrics are never shown as if
  they were synchronized.
- **Keyboard controls** without pressing Enter: pause, seek, volume, toggle lyrics/video.
- **Resilient**: reconnects dropped YouTube video streams, keeps playing audio if the picture fails,
  refits on terminal resize, shuts mpv down on every exit path, and never leaves the terminal broken.
- **Configurable** through a commented `config.toml` – no source editing needed.
- **Diagnosable**: `--doctor` environment check, rotating log file with credentials/URL tokens
  redacted, and performance statistics for every session.
- Plays local audio files too (`.mp3`, `.flac`, …), shows lyrics, and has a silent `--no-audio` mode
  that needs no mpv.

## Requirements

| Software | Needed for | Notes |
|---|---|---|
| Windows 10/11 + Windows Terminal | the supported platform | classic `conhost` works but is slower |
| Python **3.12+** | running the app | developed and tested on 3.14 |
| **mpv** | audio, and the master clock | required for normal playback |
| FFmpeg (`ffprobe`) | embedded tags/duration of local files | optional; everything plays without it |
| Internet | YouTube, lyrics | local files play offline (no lyrics) |

Python packages (OpenCV, NumPy, Rich, httpx, yt-dlp) are installed by `pip`.

## Installation

### 1. Python

Install Python 3.12 or newer from <https://www.python.org/downloads/> (tick *Add python.exe to PATH*),
then check it:

```powershell
python --version
```

### 2. mpv (required)

```powershell
winget install shinchiro.mpv
```

Alternatives: `scoop install mpv`, or download a build from <https://mpv.io/installation/> and unzip it
anywhere. **Open a new terminal** afterwards. If `mpv` is still not found, either add its folder to
`PATH` or tell the app where it is (`--mpv-path "C:\tools\mpv\mpv.exe"`, or `mpv_path` in the config).
The app also looks in the usual install folders (`Program Files\MPV Player`, Scoop, …) by itself.

### 3. FFmpeg (optional)

```powershell
winget install Gyan.FFmpeg
```

With FFmpeg's `ffprobe` available, local files supply their embedded title/artist/duration, which
improves lyrics matching. Without it, the file name is used as the title (`Artist - Title.mp4` works well).

### 4. The application

```powershell
cd path\to\cli-music-player
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

For an exactly reproducible environment use the pinned versions this release was tested with:

```powershell
pip install -r requirements-lock.txt
pip install -e . --no-deps
```

(`pip install -e ".[dev]"` additionally installs pytest and mypy.)

### 5. Check everything

```powershell
python run.py --doctor
```

You should see a ✓ for Python, OpenCV, NumPy, Rich, httpx and mpv. `!` lines are optional extras.
The installed command `ascii-music-player` is equivalent to `python run.py`.

## Running

```powershell
python run.py                                          # asks: Paste YouTube URL or media path:
python run.py "C:\Videos\Artist - Title.mp4"           # local video
python run.py "C:\Music\song.mp3"                      # local audio (no picture)
python run.py "https://www.youtube.com/watch?v=VIDEO_ID"
python run.py "C:\Videos\clip.mp4" --artist "Band" --title "Song"   # when the file name has no title
python run.py --no-audio assets\samples\demo.avi       # silent test clip, no mpv needed
```

In interactive mode the app asks for another track after each one ends (`q` quits). Paths can be
quoted or dragged onto the window.

### Keyboard controls

| Key | Action |
|---|---|
| `Space` | pause / resume |
| `←` / `→` | seek back / forward 5 seconds |
| `↑` / `↓` | volume +5 / −5 |
| `1` / `2` / `3` | quality **low** / **medium** / **high**, switched instantly while playing |
| `L` | show / hide lyrics |
| `V` | show / hide the ASCII video |
| `Q` or `Ctrl+C` | quit |

### Quality: more characters, more detail

Quality is about **how much picture detail you see**. Press `1`, `2` or `3` while playing to switch instantly.

| Key | Quality | What you get | Picture in a 120×40 window\* |
|---|---|---|---|
| `1` | **low** | chunky picture: every sample is drawn as a 2×2 block of characters; 10-level ramp; cheapest | 86×24 characters from 516 samples |
| `2` | **medium** (default, same as `balanced`) | every terminal cell is its own sample; 24-level ramp; brightness range stretched so details use the whole ramp | 85×24 = 2,040 characters |
| `3` | **high** | picture-first layout (thin borderless bars, so the picture gets far more rows and columns); every cell a sample; 70-level ramp; local contrast and sharpening | 117×33 = 3,861 characters |

\* Measured on a real video frame with the lyrics panel shown. Bigger windows give bigger pictures; the three
levels keep their order in every window size (this is covered by tests).

The picture is always fitted to the window, and redraws are capped (low 15, medium/high 30 per second): the
player shows every video frame, or every 2nd/3rd/… frame when the video's frame rate is higher than the cap
(for example every 2nd frame of 60 fps video at 30 redraws/s), so motion always advances in even steps. If the
terminal is too slow it shows fewer frames automatically, still evenly.

Start with a level using `-q low|medium|high` (`medium` and `balanced` are the same profile), or set
`quality` in the config file. Add `--color` for coloured output (heavier on the terminal).

## Configuration

Settings come from three layers, later ones winning: **built-in defaults → config file → command-line
flags.** Create a commented config file with:

```powershell
python run.py --init-config      # creates %APPDATA%\ascii-music-player\config.toml (never overwrites)
python run.py --show-config      # shows the effective settings and where they came from
```

The repository also contains [`config.example.toml`](config.example.toml). Use another file with
`--config FILE` or the `ASCII_MUSIC_PLAYER_CONFIG` environment variable.

| Section | Key | Default | Meaning |
|---|---|---|---|
| `[video]` | `quality` | `"balanced"` | `low`, `balanced`, `high` |
| | `width` | `0` | max picture width in characters, `0` = automatic from the quality |
| | `color`, `invert` | `false` | colour output; invert brightness for light themes |
| | `stabilize` | `true` | ignore tiny frame-to-frame brightness noise so still areas do not shimmer |
| | `charset` | `""` | custom ramp, darkest → brightest (`""` = the quality's ramp) |
| `[audio]` | `volume` | `80` | start volume 0–130 |
| | `volume_step`, `seek_step` | `5`, `5` | key step sizes (percent / seconds) |
| | `mpv_path` | `""` | full path to `mpv.exe` if not found automatically |
| `[lyrics]` | `enabled` | `true` | look lyrics up |
| | `show` | `true` | show the lyrics panel at start |
| | `timeout` | `20` | seconds to wait for the lyrics service (runs in the background) |
| | `api_url` | LRCLIB | lyrics API endpoint |
| | `cache_max_entries` | `500` | tracks kept in the on-disk lyrics cache (`0` = no caching) |
| `[ui]` | `show_video` | `true` | show the picture at start |
| | `fps` | `0` | max redraws per second, `0` = automatic |
| | `alt_screen` | `true` | use the terminal's alternate screen (keeps your scrollback clean) |
| `[paths]` | `cache_dir` | `""` | lyrics cache + logs folder (`""` = `%LOCALAPPDATA%\ascii-music-player\cache`) |
| `[logging]` | `enabled`, `level` | `true`, `"INFO"` | rotating log file |

Mistakes are reported with the offending key (`[audio] volume must be between 0 and 130`), and unknown
keys produce a warning instead of being silently ignored. All command-line options are listed by
`python run.py --help`.

## Performance

Measured on the development PC (Windows 11, 20 logical CPUs); see the benchmark document for the method
and the caveats. All figures are from the scripts in `scripts/`.

- **Frame conversion is cheap:** converting a 720p frame to ASCII takes about 0.04 ms (`low`) to 1.5–2 ms
  (`medium` / `high`, including contrast enhancement) and drawing the whole screen 1–2 ms, even for the
  roughly 10,000-character picture of a large window in `high` (under 4 ms in total, against the 33 ms frame period
  of 30 fps video). The converter's temporary memory per frame is 3–15 KiB depending on quality (it was 72 KiB),
  and stays flat over thousands of frames.
- **Decoding dominates CPU.** The decoder thread decodes a bounded few frames ahead; on 720p video this cut
  its CPU from about 39 % to 25 % of a core, and whole-session CPU by about 18 % in alternating before/after
  runs (51 % vs 62 % of one core). For 360p video (typical YouTube streams) there is no measurable difference
  and total CPU is around 20 % of one core.
- **Sync:** the picture on screen stays within about one video frame (median 22–25 ms, p95 about 40 ms) of the audio clock.
- **Memory:** flat over 14 consecutive sessions (no leak visible); worker threads are verified gone after every session.

Full methodology, tables and caveats: [docs/BENCHMARKS.md](docs/BENCHMARKS.md). Reproduce with
`python scripts/benchmark.py --e2e --repeat 4`.

## Troubleshooting

**Start with `python run.py --doctor`.** It checks Python, the packages, mpv, FFmpeg, the terminal, the
config file and the cache folder, and prints how to fix each problem.

| Symptom | Cause / fix |
|---|---|
| `mpv was not found` | Install mpv (see above) and open a **new** terminal, or pass `--mpv-path`. `--no-audio` plays a local video without it. |
| Lyrics panel says *No track title to search with* | The file has a generic name such as `videoplayback.mp4`. Rename it `Artist - Title.mp4` or pass `--artist` and `--title`. |
| *Lyrics service is slow or unreachable* | LRCLIB can be slow or down. Playback is unaffected; try again later. Successful lookups are cached. |
| *No lyrics found* / *not time-synchronized* | The service has none (or only plain text, which is never shown as synced). |
| YouTube: *unavailable / private / requires sign-in* | The app only plays publicly accessible videos; it does not log in or bypass restrictions. Play a local file instead. |
| YouTube suddenly fails for every video | YouTube changes often: `pip install -U yt-dlp`. |
| Picture freezes or stutters, audio keeps playing | Slow terminal or very large window. Try `-q low`, `--ui-fps 12`, or a smaller window. The end-of-session message reports stalls; details are in the log. |
| Picture shakes, shimmers or jumps ("old TV") | Three things are done against this: the player shows frames at an even cadence, holds a 1 ms Windows timer while playing for steady frame timing, and calms compression noise (`stabilize`, on by default). Try `-q low` (coarser characters shimmer less), a smaller window, or `--no-stabilize` to compare. If it persists, report the video's frame rate and what moves (whole picture / characters / panels). |
| Picture drifts from the sound | Report it with the log file (`--show-config` shows where it is). Sync errors are measured in the log. |
| Garbled characters, odd borders | Use Windows Terminal with a UTF-8 capable font (Cascadia Mono). The classic console host has limited glyph support. |
| `--color` looks wrong or is slow | Needs a 24-bit-colour terminal (Windows Terminal). Use mono output otherwise. |
| Config error on start | The message names the key; fix or delete the file (`--show-config` prints its path). |
| Leftover `mpv.exe` | Should not happen (it is closed on every exit path, even a forced kill on Windows); if it does, please report it and end the process in Task Manager. |

**Logs** are written to `<cache folder>\logs\player.log` (rotating, 1 MB × 3). They contain session
timings, errors and dependency checks; URLs are shortened to host and path, and tokens, cookies and
credentials are redacted. Nothing is ever printed to the console while playing. Use `--log-level debug`
for more, `--no-log` to disable.

## Architecture

```
                    ┌─────────────── mpv (headless audio process) ───────────────┐
 YouTube URL  ───►  │ JSON IPC over a named pipe: time-pos · pause · duration ·   │
 or local file      │ volume · end-of-file                                        │
      │             └──────────────────────────┬─────────────────────────────────┘
      ▼                                        ▼
 MediaResolver ───► ResolvedMedia        PlaybackClock  ◄── the only timeline
 (yt-dlp, ffprobe)       │                  │        \
                         ▼                  ▼         ▼
                  PlaybackSession ──► BackgroundVideoSource   LyricsSynchronizer
                   (UI thread)        (decode thread, bounded   (line for clock t;
                         │             look-ahead buffer;        fetched on its own
                         │             frame for clock t)        thread from LRCLIB)
                         ▼
              ASCIIConverter ─► Rich Live screen ◄─ KeyReader (non-blocking)
```

| Package | Responsibility |
|---|---|
| `src/cli` | argument parsing, config layering, interactive workflow, `--doctor` |
| `src/core` | `AppConfig`, quality profiles, config-file loading/validation, exceptions |
| `src/media` | URL validation, yt-dlp stream resolution, local-file probing, metadata cleanup |
| `src/audio` | mpv process + IPC client, `PlaybackClock`, `AudioController` (pause/seek/volume) |
| `src/lyrics` | LRC parser, time→line synchronizer, LRCLIB client, bounded on-disk cache |
| `src/video` | loader, resize/grayscale, ASCII mapping, clock-driven frame selection, decode-ahead |
| `src/player` | `PlaybackSession`: wires everything, owns lifecycle and cleanup |
| `src/ui` | Rich screen and loading card, colours, non-blocking keyboard input |
| `src/utils` | logging with redaction, paths/platform folders, dependency checks |

How synchronization works: mpv reports its position; the clock extrapolates at most 250 ms past the
last report and re-anchors on every new one. The decoder thread keeps a small bounded buffer of
upcoming frames (at most 8 frames and 32 MiB); the UI thread asks the clock which frame is due and
shows exactly that one, dropping older frames when behind – so a slow terminal skips frames instead of
falling behind, and video decoding or terminal drawing can never delay the audio, which plays in a
separate process. A dropped network stream is reopened at the current position with back-off.

## Development

```powershell
pip install -e ".[dev]"
python -m pytest                       # automated tests (no network, mpv or YouTube needed)
python -m mypy                         # strict type check
python scripts/benchmark.py --e2e      # performance benchmark
python scripts/demo_capture.py         # text rendering of the screen
```

The pre-release checklist for things automation cannot prove (real terminal, real mpv, real network) is
in [docs/MANUAL_TESTING.md](docs/MANUAL_TESTING.md). Release history: [RELEASE_NOTES.md](RELEASE_NOTES.md).

An optional Windows executable folder can be built with `scripts\build_exe.ps1` (PyInstaller, one-folder
build of about 190 MB). It was built and smoke-tested during development: `--version`, `--doctor`, silent
playback, real audio playback with mpv, lyrics lookup, YouTube resolution and the force-kill cleanup all
worked from the executable. mpv and FFmpeg still have to be installed separately. The executable is not
part of the tested install path (`pip install -e .`), so treat it as a convenience.

## Known limitations

- **Real-world coverage.** The automated tests use fakes for mpv, YouTube and the lyrics service.
  Real mpv, real YouTube streams and the real LRCLIB API were exercised in targeted checks during
  development, but keyboard handling in a live terminal, long soak runs and the classic console host
  have not been verified systematically – use the manual checklist.
- **YouTube** video frames are read by OpenCV from a public stream URL (about 360p, H.264 preferred) and
  audio is played by mpv from a separate stream. Both depend on yt-dlp keeping up with YouTube; some
  videos are not accessible by design (private, restricted, DRM, live streams).
- **Lyrics** matching relies on title/artist (messy YouTube titles can miss) and a ±6 s duration check.
  LRCLIB's search can be slow. Only synchronized lyrics are displayed.
- **Sync accuracy** is about one video frame (≈ 33 ms at 30 fps) by construction; there is no
  per-machine audio/video latency calibration, and the terminal's own display latency is not measured.
- **Terminals:** designed for Windows Terminal. Colour mode and large windows put real load on the
  terminal; the app throttles redraws automatically but a slow terminal will show fewer frames.
- **Packaging:** the importable package is named `src` (required by the project layout), so install
  it into a virtual environment, not globally. mpv must be installed separately. Only Windows is a
  supported target; the Linux/macOS code paths (Unix socket IPC, termios keys) are untested.
- mpv is terminated on every exit path: normal exit, errors, Ctrl+C, interpreter exit, and even a
  forced kill of the app (*End task*) thanks to a Windows job object. The forced-kill case is covered by
  a test that runs the real mpv; on non-Windows systems a forced kill can still leave mpv behind.

## Copyright and media-access considerations

- This program does **not** include or distribute any media or lyrics. It plays files you already have
  and, for YouTube, the publicly accessible streams of a video you choose – the same data a browser
  receives – through the open-source `yt-dlp` library.
- It does **not** log in, use cookies, or attempt to bypass sign-in or age checks, regional
  restrictions, DRM or any other access control, and it does not save or download media to disk.
  Videos that need any of these are reported as unavailable.
- You are responsible for complying with the terms of service of the sites you use (including
  [YouTube's Terms of Service](https://www.youtube.com/t/terms)) and with the copyright law that
  applies to the media you play.
- Lyrics are fetched from the public [LRCLIB](https://lrclib.net) API, which provides community
  contributed lyrics. Lyrics may be copyrighted; they are shown only inside your terminal during playback and a
  copy is cached locally on your machine (bounded to 500 tracks by default; set `cache_max_entries = 0`
  in the `[lyrics]` config section, or delete the cache folder, to remove them). They are not redistributed.
- Respect LRCLIB's usage expectations: the app identifies itself with a generic user agent, queries
  once per track, and caches results to avoid repeated requests.

## License

MIT – see [LICENSE](LICENSE).
