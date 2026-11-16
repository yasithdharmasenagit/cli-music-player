# Manual integration testing checklist

The automated suite (`python -m pytest`) uses fakes for mpv, YouTube and the lyrics service, so it
cannot prove that the real pieces work together **in a real terminal**. Run this checklist before a
release. Tick each box and note anything unexpected in the results table at the end.

Run it in **Windows Terminal with PowerShell**, then repeat the starred (★) items in the classic
console host (`conhost`/legacy PowerShell window), which has fewer features.

Test media you will need:

- `A` – a local music video file (`.mp4`/`.mkv`) named `Artist - Title.mp4`, whose song has synced
  lyrics on [lrclib.net](https://lrclib.net).
- `B` – a local video named `videoplayback.mp4` (or any generic name, so no lyrics can be found).
- `C` – a local audio file (`.mp3`/`.flac`).
- `U` – a public YouTube music-video URL.
- `P` – a private or removed YouTube URL.

## 0. Environment

- [ ] `python run.py --doctor` shows ✓ for Python, OpenCV, NumPy, Rich, httpx and mpv (FFmpeg and
      yt-dlp may be `!`/optional). Exit code is 0 (`echo $LASTEXITCODE`).
- [ ] `python run.py --version` prints `ascii-music-player 1.0.0rc1`.
- [ ] `python run.py --show-config` prints the effective settings.

## 1. Startup and input

- [ ] ★ `python run.py` shows the banner, `Paste YouTube URL or media path:` and a `>` prompt.
- [ ] Typing `q` at the prompt exits with code 0 and a clean terminal.
- [ ] Pasting a path with quotes (`"C:\...\A.mp4"`) and dragging a file onto the window both work.
- [ ] Pressing Ctrl+C at the prompt exits cleanly (no traceback).
- [ ] An empty line, a missing file, `https://vimeo.com/1` and a `.txt` file each give a one-line error
      and the prompt returns (interactive mode) – no traceback.

## 2. Local playback (`A`)

- [ ] ★ The loading card appears (spinner, checklist) for a moment, then the player screen.
- [ ] Header shows title and artist; the ASCII picture is animated and fills the window sensibly.
- [ ] Audio plays; the picture and the sound stay together (clap/cut/lyric scenes line up) for the
      whole song – note any drift you can see or hear.
- [ ] Time and the progress bar advance; the status reads `▶ Playing`.
- [ ] Lyrics are found within ~20 s and the current line highlights in step with the singing.
- [ ] Previous and next lyric lines are shown dimly around the current one.
- [ ] When the song ends the player exits, the terminal is restored (scrollback intact, cursor
      visible) and the prompt returns.

## 3. Controls

- [ ] ★ `Space` pauses audio and picture (status `‖ Paused`); `Space` again resumes in sync.
- [ ] `→` jumps 5 s forward and `←` 5 s back: audio, picture and lyrics all follow; a brief
      notice (`5s ▶▶` / `◀◀ 5s`) appears.
- [ ] `↑`/`↓` change the volume in steps of 5 and the bar and percentage update.
- [ ] `1`, `2`, `3` switch quality low / medium / high instantly (detail, width and smoothness change;
      a `Quality: …` notice appears and the footer shows the current quality).
- [ ] `L` hides/shows the lyrics panel and the picture refits; `V` hides/shows the video.
- [ ] `Q` quits immediately; `Ctrl+C` also quits cleanly.
- [ ] Holding a key down (key repeat) does not freeze or flood the player.

## 4. Other media

- [ ] Audio-only file `C`: shows `♪ Audio only`, lyrics still work, controls work.
- [ ] File `B` (generic name): playback works; the lyrics panel explains that there is no usable
      title (`--title`/`--artist` hint). `python run.py B --artist "X" --title "Y"` searches with those.
- [ ] YouTube `U`: resolves, plays audio and picture in sync, lyrics appear.
- [ ] YouTube `P` (private/removed): a clear message and the suggestion to play a local file.
- [ ] Unplug the network (or disable Wi-Fi) mid-way through `U`: the player shows
      `Video stream interrupted; reconnecting…` and recovers after the network returns, or ends with a
      clear error. It must not freeze the screen or the keys.

## 5. Lyrics edge cases

- [ ] Start `A` with the network disabled: playback works; the panel says the lyrics service is
      unavailable; no crash.
- [ ] Play `A` twice: the second run is instant for lyrics (served from the cache folder).
- [ ] `python run.py A --no-lyrics` shows no lyrics panel activity.

## 6. Robustness and cleanup

- [ ] While `A` plays, kill `mpv.exe` in Task Manager: the player ends with
      `mpv stopped unexpectedly` and restores the terminal.
- [ ] While `A` plays, end the **python.exe** of the app with *End task*: `mpv.exe` disappears too.
- [ ] After every way of ending (finish, `Q`, Ctrl+C, killed mpv) Task Manager shows **no**
      leftover `mpv.exe` and no leftover `python.exe` for this app.
- [ ] ★ Resize the window (smaller, larger, maximised) during playback: the picture refits within
      a fraction of a second, with no garbage left on screen.
- [ ] A very small window (≈ 40×12) does not crash.
- [ ] `python run.py` and play three different files in a row (interactive mode): each works and
      memory (Task Manager) does not climb noticeably.
- [ ] Play a damaged/truncated video file: the player falls back to audio-only or stops the picture
      cleanly, audio still plays to the end.

## 7. Quality, configuration and logs

- [ ] Keys `1` / `2` / `3` (or `-q low|medium|high`) visibly change the picture: low = chunky blocks, medium =
      full-resolution grid, high = a bigger picture (thin bars, more rows/columns) with more character detail;
      `low` uses the least CPU (Task Manager). Try a dark stage video: medium/high should show more detail than low.
- [ ] `--color` shows a coloured picture; `--no-color` returns to mono.
- [ ] `python run.py --init-config` creates the config file; editing `volume = 30` takes effect;
      a deliberate typo like `volume = "loud"` prints an error naming `[audio] volume` and exits 2.
- [ ] The log file (`--show-config` shows the cache folder → `logs\player.log`) contains the session
      start/end lines with performance statistics and **no** URLs with tokens, cookies or credentials.
- [ ] `python run.py --no-audio assets\samples\demo.avi` plays silently without mpv.

## 8. Soak test (optional)

- [ ] Play a 30+ minute video once. Time and lyrics stay in sync to the end; memory stays flat.

## Results

| Date | Tester | Terminal | Windows version | Items failed / notes |
|------|--------|----------|-----------------|----------------------|
|      |        |          |                 |                      |

## What the automated suite already covers (so you can focus on the real-world parts)

Option layering and config validation, URL/path validation, LRC parsing and lyric timing, LRCLIB
requests/timeouts/outages (mocked), the mpv IPC protocol and process lifecycle (fake pipe/process, plus a
one-off check against a real Windows named pipe), the playback clock, video-follows-clock selection
including seeks, jumps, stream drops and corrupt files, thread/process cleanup, terminal restore
sequences, log redaction, and the interactive workflow.
