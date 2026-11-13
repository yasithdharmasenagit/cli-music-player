# ASCII Music Player

Paste a YouTube link or a local file path and watch it as animated ASCII art, with audio and
synchronized lyrics, entirely inside your terminal (built for Windows Terminal / PowerShell).

- **Phase 1:** ASCII video rendering engine.
- **Phase 2 (this version):** audio via mpv, YouTube support, LRCLIB lyrics, combined player UI, keyboard controls.

## Requirements

| Software | Needed for | Install (Windows) |
|---|---|---|
| Python 3.12+ | everything | https://www.python.org/downloads/ |
| **mpv** | audio playback and the master clock | `winget install shinchiro.mpv`, `scoop install mpv`, or https://mpv.io/installation/ |
| FFmpeg (optional) | embedded tags/duration of local audio files via `ffprobe` | `winget install Gyan.FFmpeg` or https://ffmpeg.org/download.html |

mpv must be on your `PATH` (restart the terminal after installing), or pass `--mpv-path C:\path\to\mpv.exe`
(or set the `MPV_PATH` environment variable). Without FFmpeg everything still plays; local audio files just
show their filename as the title and may lack a duration.

Python packages (OpenCV, NumPy, Rich, httpx, yt-dlp, ...) are installed by pip.

## Installation (PowerShell)

```powershell
cd path\to\cli-music-player
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## Usage

```powershell
python run.py                                   # prompts: Paste YouTube URL or media path:
python run.py "https://www.youtube.com/watch?v=VIDEO_ID"
python run.py "C:\Music\Videos\song.mp4"
python run.py "C:\Music\song.mp3"               # audio files work too (no picture)
```

Workflow:

```
ASCII MUSIC PLAYER
Watch music. Differently.

Paste YouTube URL or media path:
>
[+] Reading media information
[+] Preparing playback
[+] Searching for lyrics
[+] Starting player
```

### Keyboard controls

| Key | Action |
|---|---|
| `Space` | pause / resume |
| `←` / `→` | seek back / forward 5 s |
| `↑` / `↓` | volume +5 / −5 |
| `L` | toggle lyrics |
| `V` | toggle ASCII video |
| `Q` (or Ctrl+C) | quit |

### Options

| Option | Description |
|---|---|
| `-w, --width N` | maximum ASCII width (default 120; always fitted to the terminal) |
| `-q, --quality {standard,high}` | character ramp detail |
| `--charset CHARS`, `--invert`, `--color` | rendering style |
| `--volume N` | initial volume 0-130 (default 80) |
| `--no-lyrics` / `--no-video` | start without lyrics lookup / with the video hidden |
| `--mpv-path PATH` | explicit mpv location |
| `--no-audio` | silent mode: local video only, no mpv required (Phase 1 player) |
| `--no-alt-screen`, `--log` | render inline; write `data/cache/player.log` |

## How it works

```
                 ┌───────────── mpv (audio, headless) ─────────────┐
 YouTube URL ─►  │ time-pos / pause / duration events over JSON IPC │
 or file path    └───────────────────────┬─────────────────────────┘
        │                                ▼
  MediaResolver                    PlaybackClock  ◄── the only timeline
  (yt-dlp / ffprobe)                /        \
        │                 SyncedVideoSource   LyricsSynchronizer
        ▼                 (frame for clock t) (line for clock t)
   ResolvedMedia ─►  PlaybackSession ─► Rich Live: header · ASCII video · lyrics · transport
                          ▲
                     KeyReader (msvcrt, non-blocking)
```

- **One clock.** mpv's `time-pos` is the authority. `PlaybackClock` stores each report and extrapolates
  at most 250 ms past it (re-anchored on every report), so video and lyrics are always chosen *from the
  audio position*, never from Python timers. `time.sleep(5 ms)` in the UI loop only yields the CPU.
- **Video follows audio.** For clock time *t* the target frame is `floor(t × fps)`. If it is the next
  frame it is read; if a few frames behind they are skipped (`grab`, no decode); a large gap or a backward
  jump (seek) uses `CAP_PROP_POS_FRAMES`. A slow terminal therefore drops frames instead of lagging.
- **Lyrics** come from [LRCLIB](https://lrclib.net) on a background thread (playback never waits). Only
  timestamped lyrics are displayed; plain-text-only results are reported as "not time-synchronized" and never
  shown as synced. Successful lookups are cached in `data/cache/lyrics/`. Extra providers can be added to
  `LyricsService` (any object with `name` and `fetch(query)`).
- **mpv IPC** runs on one I/O thread over a Windows named pipe (Unix socket elsewhere) using
  `PeekNamedPipe`, so reads never block command writes.

### YouTube and permitted access

The app asks `yt-dlp` for the metadata and the public stream URLs of the single video you pasted, and
plays those. It does not use cookies or logins, and does not attempt to bypass sign-in, age checks,
region locks or DRM; stream URLs (which embed access tokens) are never logged or printed. If a video
isn't accessible, the app says why and suggests playing a local file instead. YouTube changes often:
if resolution suddenly fails, run `pip install -U yt-dlp`. Respect YouTube's Terms of Service and copyright
law for anything you play.

## Tests and checks

```powershell
python -m pytest        # 174 tests, no network, mpv or YouTube required
python -m mypy          # strict type check
```

Tests use mocks for mpv (a fake IPC transport and a fake audio engine that feeds the clock), yt-dlp
(injected extractor) and LRCLIB (`httpx.MockTransport`).

## Manual testing checklist

1. `python run.py assets\samples\demo.avi --no-audio`: silent ASCII video (no mpv needed).
2. Install mpv, then `python run.py assets\samples\demo.avi`: the demo has no sound, so check
   the layout, progress bar and keys (Space, arrows, `L`, `V`, `Q`).
3. Play a real music video file (`.mp4`/`.mkv`) with a file name like `Artist - Title.mp4`: lyrics should
   appear within a few seconds and follow the audio. Press `→`/`←` and confirm picture and lyrics jump with the audio.
4. Paste a public YouTube music video URL; try a private/removed one to see the error message.
5. Resize the window during playback; the picture refits.
6. Press `Q`: the terminal returns to normal; check Task Manager for no stray `mpv.exe`.

## Project layout

```
src/
  cli/     main.py (workflow), interface.py (prompts/messages)
  core/    config.py (AppConfig), exceptions.py
  media/   metadata.py, youtube_resolver.py, media_resolver.py
  audio/   playback_clock.py, audio_player.py (mpv + IPC), audio_controller.py
  lyrics/  lyrics_models.py, lyrics_parser.py (LRC), lyrics_sync.py, lyrics_client.py (LRCLIB, cache)
  video/   video_loader.py, frame_processor.py, ascii_converter.py, video_player.py (Phase 1), video_sync.py
  player/  session.py (ties everything together)
  ui/      player_view.py, keyboard.py, terminal_renderer.py (Phase 1), layout.py, colors.py
  utils/   logger.py, paths.py, dependencies.py
```

## Known limitations

- Not yet verified against a real mpv installation or live YouTube/LRCLIB from this repository's CI-less
  environment; see the manual checklist. The named-pipe transport and IPC client were verified against a
  hand-written named-pipe server on Windows.
- YouTube video frames are read by OpenCV straight from a stream URL (360p-ish, H.264 preferred); seeking
  in network streams can be slow, and some OpenCV builds cannot open every stream.
- Audio/video offset from decode latency is not auto-calibrated.
- Lyrics matching relies on the title/artist (YouTube titles can be messy) and a ±6 s duration check.
- `--color` is heavier on the terminal at large widths.
