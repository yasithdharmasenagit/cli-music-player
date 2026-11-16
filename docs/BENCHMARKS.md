# Performance benchmarks

All numbers below were **measured on one machine** with the scripts in `scripts/`; nothing is estimated.
Absolute values depend on the machine and change from run to run (see *Caveats*). Use them to judge
relative behaviour, and re-measure on your own PC with the commands at the end.

## Test setup

- Machine: Windows 11, 20 logical CPUs, Python 3.14.6, OpenCV 5.0.0, NumPy 2.5.3.
- Source video (generated locally with a fixed random seed, no downloads): 1280x720 MJPG synthetic, 120 frames. A 640x360 clip is used for the
  "typical YouTube" case. MJPG is used because it needs no external codec; H.264 streams decode differently.
- Terminal output goes to an in-memory console (no real terminal is drawn), so these numbers exclude the
  terminal emulator's own rendering time. mpv is replaced by a fake that feeds the clock in real time, so audio
  decoding is not included either (it runs in a separate process).
- CPU % = process CPU time (all threads) / wall time; 100 % = one full core.

## 1. Per-frame pipeline cost by terminal width (milliseconds, median)

Source frames are 1280x720 BGR. `terminal` is the time to build the full Rich screen (header, picture, lyrics, footer)
and write it to an in-memory terminal. Decoding is not part of this table (it measured 4.9 ms median per frame in the same run).

**low**

| width | grid | resize + gray | char map | ASCII total | terminal | ASCII + terminal p95 |
|---:|---:|---:|---:|---:|---:|---:|
| 60 | 60x17 | 0.01 | 0.002 | 0.02 | 1.24 | 2.24 |
| 80 | 80x22 | 0.02 | 0.003 | 0.02 | 1.01 | 1.38 |
| 120 | 120x34 | 0.03 | 0.004 | 0.03 | 1.16 | 1.64 |
| 160 | 160x45 | 0.03 | 0.005 | 0.04 | 1.44 | 2.18 |
| 200 | 178x50 | 0.04 | 0.005 | 0.05 | 1.52 | 2.26 |

**balanced**

| width | grid | resize + gray | char map | ASCII total | terminal | ASCII + terminal p95 |
|---:|---:|---:|---:|---:|---:|---:|
| 60 | 60x17 | 1.25 | 0.003 | 1.25 | 0.92 | 3.21 |
| 80 | 80x22 | 1.21 | 0.003 | 1.21 | 1.03 | 3.29 |
| 120 | 120x34 | 1.26 | 0.003 | 1.26 | 1.21 | 3.35 |
| 160 | 160x45 | 0.58 | 0.004 | 0.59 | 1.48 | 2.78 |
| 200 | 178x50 | 1.40 | 0.005 | 1.41 | 1.49 | 4.40 |

**high**

| width | grid | resize + gray | char map | ASCII total | terminal | ASCII + terminal p95 |
|---:|---:|---:|---:|---:|---:|---:|
| 60 | 60x17 | 1.26 | 0.002 | 1.26 | 0.92 | 3.73 |
| 80 | 80x22 | 1.20 | 0.003 | 1.20 | 1.00 | 4.14 |
| 120 | 120x34 | 1.26 | 0.003 | 1.27 | 1.20 | 4.70 |
| 160 | 160x45 | 0.55 | 0.004 | 0.56 | 1.45 | 2.97 |
| 200 | 178x50 | 1.36 | 0.005 | 1.37 | 1.52 | 4.92 |

Reading the table: at every width the whole ASCII conversion plus drawing is a few milliseconds, far below the 33 ms
frame period of 30 fps video, so conversion is not the bottleneck; video decoding is (see section 3). `low` is the
cheapest because it uses a linear resize filter instead of the anti-aliased area filter. (Width 160 is faster than its neighbours
only because 1280 divides evenly by 160, which OpenCV handles on a fast path; it is a property of this test video.)

## 2. Memory allocated per converted frame

Python-level allocations (`tracemalloc`; OpenCV's native buffers are not included), peak extra memory while
converting 100 frames, `high` ramp. Deterministic, so it is not affected by machine drift.

| width | before (phase 2 code) | after |
|---:|---:|---:|
| 60 | 20.8 KiB | 2.3 KiB (89 % less) |
| 120 | 71.6 KiB | 5.4 KiB (93 % less) |
| 200 | 146.1 KiB | 10.1 KiB (93 % less) |

The remaining allocation is essentially the output string itself.

## 3. Whole-session CPU, before vs after (paired, alternating runs)

CPU speed drifts during a session of measurements (during development the same video decoded at 5.2 ms/frame
in one run and 8.2 ms/frame in another), so measuring "before" and then "after" is unreliable: a first attempt
that did exactly that was discarded because the machine was visibly slower during the second half.
`scripts/compare_versions.py` therefore alternates old and new runs in fresh processes (8 s sessions, `high`
quality, 100 columns, 160x45 terminal) and records the machine's decode speed during each run.
"Before" is the phase 2 code (commit `bd6fb31`); "after" is this release.

**1280x720 source (decoding is expensive):**

| pair | before CPU | after CPU |
|---:|---:|---:|
| 1 | 50 % | 41 % |
| 2 | 53 % | 62 % |
| 3 | 69 % | 48 % |
| 4 | 66 % | 54 % |
| 5 | 73 % | 63 % |
| 6 | 59 % | 48 % |

Median **before 62 %** (range 50-73) vs **after 51 %** (range 41-63); paired ratio per pair 0.81, 1.16, 0.70, 0.82, 0.86, 0.82,
**median 0.82** (about 18 % lower; 5 of 6 pairs lower). Picture-vs-audio sync error (median of samples of the frame on screen vs
the audio clock): **36 ms before, 24 ms after**, p95 about 55 ms before and 42 ms after.

**640x360 source (typical YouTube stream):** before median 17 % (range 15-22), after median 19 % (range 12-30);
paired ratios 1.23, 0.72, 1.57, 0.79, 1.18, 1.33 (median 1.21). **No improvement is measurable here**: the difference is within
the run-to-run noise (the decode-thread-only test below shows no cost from the new buffer at this size). Sync error median 27 ms before, 23 ms after.

**Decode thread in isolation, CPU of one core** (7 alternating runs each, median; 6 s of 30 fps video):

| look-ahead buffer | 640x360 | 1280x720 |
|---|---:|---:|
| none (decode one frame per frame period) | 12.7 % | 39.2 % |
| 4 frames | 12.7 % | 29.8 % |
| **8 frames (default)** | **12.4 %** | **24.9 %** |

Why it helps: decoding a few frames back-to-back and then idling costs less CPU than waking up for every frame
(warm caches, fewer wake-ups). The buffer is bounded (8 frames and 32 MiB, fewer for very large frames) and frames are
handed to the screen only when the clock says they are due; delivery was checked to be complete (every frame, in order,
median 6 ms after its due time).

## 4. Whole-session CPU by quality and terminal size (this release)

Median of 4 runs of 8 s on the 1280x720 source (min-max in brackets). These runs were taken in one batch while the
machine was in a slow period (decode was 7.2 ms/frame), so absolute CPU is higher than in section 3; use them to compare
qualities and sizes with each other, not with section 3.

| quality | 100x30 | 160x45 | 220x60 |
|---|---:|---:|---:|
| low | 44 % (34-49) | 53 % (36-67) | 57 % (48-60) |
| balanced | 69 % (62-72) | 70 % (65-74) | 75 % (68-77) |
| high | 74 % (69-81) | 73 % (69-78) | 81 % (78-85) |

Across all of these runs: 0 skipped frames, 0 seeks, 0 stalls; the picture stayed within a median of 22-25 ms (p95 40-44 ms) of the audio clock, which is about one video frame - the resolution of frame selection at 30 fps.

## 5. Memory and leaks

14 consecutive 4-second sessions in one process (`--leak 14`): resident memory 62.6 MiB after the first, 63.6 MiB after the last; mean of the first half 62.7 MiB, of the second half 63.4 MiB; growth per session in the second half +0.00 MiB; threads 2 before and 2 after. No leak is visible. The automated tests additionally check that no decoder, mpv-IPC or lyrics thread survives any way of ending a session.

## 6. Idle cost

A session without video (audio only) used 1-5 % of one core in three measurements after the change, versus 5.7 % in the
single measurement before it (the UI loop now sleeps until something can change instead of polling every 5 ms).
These are small numbers on a noisy clock, so treat them as indicative only.

## 7. Quality levels: detail versus cost

The quality levels were redesigned to change how much picture you see (resolution, layout, enhancement), so their cost
was measured on the 1280x720 test video. Per frame, median milliseconds: `convert` = resize + enhancement +
shimmer suppression + character mapping; `terminal` = build the whole Rich screen and write it to an in-memory
terminal (so it excludes the terminal emulator's own drawing time). Machine in its normal state (decode 4.9 ms/frame).

| window | quality | convert | terminal | total | characters on screen |
|---|---|---:|---:|---:|---:|
| 100x30 | low | 0.04 | 0.94 | 0.98 | 700 |
| 100x30 | medium | 1.46 | 1.07 | 2.53 | 700 |
| 100x30 | high | 1.69 | 1.05 | 2.74 | 1,886 |
| 120x40 | low | 0.04 | 1.07 | 1.12 | 2,064 |
| 120x40 | medium | 1.47 | 1.22 | 2.69 | 2,040 |
| 120x40 | high | 1.76 | 1.26 | 3.03 | 3,861 |
| 160x50 | low | 0.05 | 1.22 | 1.26 | 2,800 |
| 160x50 | medium | 1.64 | 1.48 | 3.12 | 4,114 |
| 160x50 | high | 1.91 | 1.46 | 3.37 | 6,579 |
| 220x60 | low | 0.05 | 1.24 | 1.29 | 2,800 |
| 220x60 | medium | 1.77 | 1.76 | 3.53 | 6,864 |
| 220x60 | high | 1.98 | 1.67 | 3.65 | 9,964 |

Reading it: `high` draws up to about 3.5x the characters of `low` (at 120x40: 3,861 vs 2,064 characters, built from 7.5x as
many real samples because low draws each sample as a 2x2 block) yet costs only about 0.3 ms more conversion than `medium`;
even the roughly 10,000-character picture of a 220x60 window needs under 4 ms per frame against a 33 ms frame budget.
`low` is deliberately capped (about 100 columns) because its point is to be cheap. The first rows of this table measure
conversion and screen building only; the emulator's own drawing of 10,000 characters per frame is not measured, which is
why the player still throttles its redraw rate automatically on slow terminals.

## Caveats

- One machine, one OS. Different CPUs, terminals and videos will differ; H.264/VP9 decode costs differ from MJPG.
- The terminal emulator's own drawing time is not measured; in a real window it adds to what the numbers show, which is why
  the player measures its own redraw time and throttles redraws on slow terminals.
- Windows accounts CPU time in scheduler ticks, so single short runs are noisy; compare medians, not single runs.
- The 360p paired comparison is inconclusive (within noise); only the 720p improvement is supported by the data.

## Reproduce

```powershell
python scripts/benchmark.py --e2e --repeat 4 --leak 14 --json results.json     # sections 1, 2, 4, 5
python scripts/compare_versions.py --old PATH_TO_OLD_CHECKOUT --pairs 6        # section 3 (paired)
python scripts/compare_versions.py --old PATH_TO_OLD_CHECKOUT --resolution 640x360
```
