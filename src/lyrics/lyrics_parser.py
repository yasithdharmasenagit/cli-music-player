"""LRC (timestamped lyrics) parser."""

from __future__ import annotations

import re

from src.lyrics.lyrics_models import LyricLine

# [mm:ss], [mm:ss.x], [mm:ss.xx], [mm:ss.xxx] (also ':' before the fraction); minutes may exceed 59
_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META = re.compile(r"^\[([A-Za-z#]+):([^\]]*)\]\s*$")
_WORD_TAG = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")  # enhanced LRC word times


def _to_seconds(minutes: str, seconds: str, fraction: str | None) -> float:
    frac = float(f"0.{fraction}") if fraction else 0.0
    return int(minutes) * 60 + int(seconds) + frac


def parse_lrc(text: str) -> list[LyricLine]:
    """Parse LRC content into time-ordered lines.

    - Several timestamps before one text (``[00:10.00][01:20.00]chorus``) yield one line each.
    - Lines sharing a timestamp keep their file order (stable sort).
    - ``[offset:+/-ms]`` is applied (positive offset makes lyrics appear earlier, per the LRC spec).
    - Metadata tags (``[ar:...]``, ``[ti:...]``) and malformed lines are ignored.
    - Empty-text lines are preserved: they mark instrumental gaps that clear the display.
    """
    lines: list[LyricLine] = []
    offset = 0.0
    for raw in text.replace("﻿", "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        stamps = list(_TIMESTAMP.finditer(raw))
        if not stamps:
            meta = _META.match(raw)
            if meta and meta.group(1).lower() == "offset":
                try:
                    offset = int(meta.group(2).strip()) / 1000.0
                except ValueError:
                    pass
            continue
        # Timestamps must be a leading run; text is whatever follows the last one.
        if stamps[0].start() != 0:
            continue
        end = 0
        times: list[float] = []
        for m in stamps:
            if m.start() != end:
                break
            if int(m.group(2)) > 59:
                break
            times.append(_to_seconds(m.group(1), m.group(2), m.group(3)))
            end = m.end()
        if not times:
            continue
        content = _WORD_TAG.sub("", raw[end:]).strip()
        for t in times:
            lines.append(LyricLine(t, content))
    adjusted = [LyricLine(max(0.0, ln.time - offset), ln.text) for ln in lines]
    adjusted.sort(key=lambda ln: ln.time)  # stable: equal timestamps keep file order
    return adjusted
