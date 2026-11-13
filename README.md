# ASCII Music Player

A terminal application that turns videos into animated ASCII art, built for Windows Terminal / PowerShell.
Phase 1 delivers the core rendering engine: local video → ASCII frames → smooth terminal playback.
(Audio, YouTube downloading and lyrics come in later phases.)

## Requirements

- Windows 10/11 with Windows Terminal (any modern terminal works)
- Python 3.12+
- No separate FFmpeg install is needed in Phase 1: OpenCV bundles its own decoder.
  (FFmpeg will be required in the audio phase.)

## Installation (PowerShell)

```powershell
cd path\to\cli-music-player
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## Usage

```powershell
python run.py                              # prompts for a path
python run.py "C:\Videos\clip.mp4"         # play directly
python run.py assets\samples\demo.avi      # bundled synthetic demo clip
```

Interactive mode:

```
ASCII MUSIC PLAYER
Watch music. Differently.

Enter media path:
>
```

Type `q` to quit. Quoted / drag-and-dropped paths are accepted. Press **Ctrl+C** during playback to stop; the terminal is restored.

### Options

| Option | Description |
|---|---|
| `-w, --width N` | ASCII width in characters (default 120; auto-shrunk to fit the terminal) |
| `-q, --quality {standard,high}` | 10-level or 70-level character ramp (default `high`) |
| `--charset CHARS` | custom ramp, dark → bright (overrides `--quality`) |
| `--fps N` | override playback FPS (default: the video's own) |
| `--color` | 24-bit ANSI colour output |
| `--invert` | invert brightness (for light terminals) |
| `--no-stats` | hide the status line |
| `--no-alt-screen` | render inline instead of the alternate screen |
| `--no-drop` | never drop frames |
| `--log` | write `data/cache/player.log` |

Tip: use a small font or maximise the window for larger `--width` values.

## Tests and checks

```powershell
python -m pytest
python -m mypy
```

Tests use synthetic frames and generated videos, so nothing needs downloading.

## Architecture

```
src/
  cli/    main.py (args + workflow), interface.py (prompts/messages)
  core/   config.py (immutable AppConfig, Quality presets), exceptions.py
  video/  video_loader.py   – open/validate/read/skip/release (OpenCV)
          frame_processor.py – validate, aspect-correct resize, grayscale
          ascii_converter.py – brightness → characters (+ optional ANSI colour)
          video_player.py    – deadline-based timing loop, frame dropping, stats
  ui/     terminal_renderer.py (Rich Live sink), layout.py, colors.py
  utils/  logger.py, paths.py
```

`VideoPlayer` knows nothing about Rich: it talks to a `FrameSink` protocol, which `TerminalRenderer` implements.
All settings live in `src/core/config.py` (`AppConfig`).

## Performance notes

- Frames are downscaled with `cv2.INTER_AREA` *before* grayscale conversion, then mapped through a 256-entry lookup table with NumPy — about 1–2 ms per frame at width 120.
- Frames are paced against absolute deadlines (`start + n/fps`); if rendering falls behind, frames are skipped (`grab()`, no decode) to stay in sync.
- Rich `Live` runs with manual refresh in the alternate screen buffer, giving one flush per frame and no flicker.

## Known limitations

- Windows Terminal is recommended; legacy `conhost` is slower and has limited colour support.
- `--color` is noticeably heavier on terminal throughput at large widths.
- Frame pacing uses `time.sleep`, which on Windows has roughly 1 ms resolution.
- No audio, YouTube, or lyrics yet (later phases).
- Codec support depends on the OpenCV build (MP4/H.264, AVI, MKV, WebM generally work).
