from __future__ import annotations

import json

import httpx
import pytest

from src.lyrics.lyrics_client import LrclibProvider, LyricsCache, LyricsQuery, LyricsService
from src.lyrics.lyrics_models import LyricLine, Lyrics, LyricsStatus
from src.lyrics.lyrics_parser import parse_lrc
from src.lyrics.lyrics_sync import LyricsSynchronizer, LyricsTrack
from src.media.metadata import MediaMetadata

# ------------------------------------------------------------------ parser


def test_parse_basic_and_precision():
    lines = parse_lrc("[00:12.34]Hello\n[01:05.5]World\n[02:00]No fraction\n[00:01.123]Ms")
    assert [(l.time, l.text) for l in lines] == [
        (1.123, "Ms"), (12.34, "Hello"), (65.5, "World"), (120.0, "No fraction")]


def test_parse_multiple_timestamps_per_line():
    lines = parse_lrc("[00:10.00][01:20.00]Chorus\n[00:30.00]Verse")
    assert [(l.time, l.text) for l in lines] == [(10, "Chorus"), (30, "Verse"), (80, "Chorus")]


def test_parse_same_timestamp_keeps_file_order():
    lines = parse_lrc("[00:05.00]Line\n[00:05.00]Translation\n[00:04.00]Before")
    assert [l.text for l in lines] == ["Before", "Line", "Translation"]


def test_parse_ignores_metadata_and_junk():
    text = "[ar:Artist]\n[ti:Title]\n[length:03:20]\nplain text\n[00:xx]bad\n\n[00:01.00]ok"
    assert parse_lrc(text) == [LyricLine(1.0, "ok")]


def test_parse_offset_and_empty_lines_and_word_tags():
    lines = parse_lrc("[offset:+500]\n[00:10.00]<00:10.00>Hel<00:10.50>lo\n[00:20.00]")
    assert lines[0] == LyricLine(9.5, "Hello")
    assert lines[1] == LyricLine(19.5, "")


def test_parse_large_minutes_bom_and_clamping():
    assert parse_lrc("﻿[100:00.00]Long")[0].time == 6000
    assert parse_lrc("[offset:+5000]\n[00:01.00]x")[0].time == 0.0
    assert parse_lrc("") == [] and parse_lrc("no timestamps") == []
    assert parse_lrc("[00:75.00]bad seconds") == []


# ------------------------------------------------------------------ synchronizer

LINES = parse_lrc("[00:10.00]One\n[00:20.00]Two\n[00:20.00]Deux\n[00:30.00]Three\n[00:40.00]\n[00:50.00]Five")


def test_index_boundaries():
    s = LyricsSynchronizer(LINES)
    assert s.index_at(0) == -1 and s.index_at(9.99) == -1
    assert s.index_at(10.0) == 0 and s.index_at(19.99) == 0
    assert s.index_at(20.0) == 1 and s.index_at(1e6) == 4


def test_window_groups_same_timestamp():
    s = LyricsSynchronizer(LINES)
    w = s.window_at(25)
    assert w.previous == ("One",) and w.current == ("Two", "Deux") and w.upcoming == ("Three",)
    before = s.window_at(1)
    assert before.current == () and before.upcoming == ("One",) and before.index == -1


def test_window_gap_clears_current_and_end():
    s = LyricsSynchronizer(LINES)
    assert s.window_at(45).current == ()  # instrumental gap
    last = s.window_at(55)
    assert last.current == ("Five",) and last.upcoming == ()


def test_empty_synchronizer():
    s = LyricsSynchronizer([])
    assert s.window_at(5).current == () and len(s) == 0


# ------------------------------------------------------------------ provider

SYNCED = "[00:01.00]Hello\n[00:02.00]World"


def provider(handler, timeout=1.0):
    return LrclibProvider("https://lrclib.test/api", timeout,
                          httpx.Client(transport=httpx.MockTransport(handler)))


def q(duration=200.0, artist="Artist"):
    return LyricsQuery("Song", artist, duration)


def test_exact_match_synced():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"duration": 201, "syncedLyrics": SYNCED})

    res = provider(handler).fetch(q())
    assert res.status is LyricsStatus.SYNCED and res.is_synced and len(res.lines) == 2
    assert calls == ["/api/get"]  # no needless second request


def test_falls_back_to_search_and_picks_closest_duration():
    def handler(request):
        if request.url.path.endswith("/get"):
            return httpx.Response(404, json={})
        return httpx.Response(200, json=[
            {"duration": 100, "syncedLyrics": "[00:01.00]wrong recording"},
            {"duration": 203, "syncedLyrics": "[00:01.00]close"},
            {"duration": 199, "syncedLyrics": "[00:01.00]closest"},
        ])

    res = provider(handler).fetch(q())
    assert res.lines[0].text == "closest"


def test_search_without_artist_or_duration_skips_get():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=[{"syncedLyrics": SYNCED}])

    assert provider(handler).fetch(LyricsQuery("Song")).is_synced
    assert paths == ["/api/search"]


def test_not_found():
    res = provider(lambda r: httpx.Response(404 if "get" in r.url.path else 200, json=[])
                   ).fetch(q())
    assert res.status is LyricsStatus.NOT_FOUND and res.status_text == "No lyrics found."


def test_instrumental():
    res = provider(lambda r: httpx.Response(200, json={"duration": 200, "instrumental": True,
                                                       "syncedLyrics": None, "plainLyrics": None})
                   ).fetch(q())
    assert res.status is LyricsStatus.INSTRUMENTAL and res.status_text == "Instrumental"


def test_plain_lyrics_never_presented_as_synced():
    res = provider(lambda r: httpx.Response(200, json={"duration": 200, "syncedLyrics": None,
                                                       "plainLyrics": "la la la"})).fetch(q())
    assert res.status is LyricsStatus.UNSYNCED and not res.is_synced and not res.lines
    assert "not time-synchronized" in res.status_text


def test_duration_mismatch_rejected():
    res = provider(lambda r: httpx.Response(200, json=[{"duration": 400, "syncedLyrics": SYNCED}]
                                            if "search" in r.url.path else {}, )).fetch(q())
    assert not res.is_synced


def test_timeout_and_network_errors_and_bad_json():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    res = provider(timeout).fetch(q())
    assert res.status is LyricsStatus.ERROR and "timed out" in res.status_text

    def refused(request):
        raise httpx.ConnectError("down", request=request)

    assert provider(refused).fetch(q()).status is LyricsStatus.ERROR
    assert provider(lambda r: httpx.Response(500)).fetch(q()).status is LyricsStatus.ERROR
    assert provider(lambda r: httpx.Response(200, content=b"<html>")).fetch(q()
                                                                       ).status is LyricsStatus.ERROR


# ------------------------------------------------------------------ service / cache

META = MediaMetadata("Song (feat. X)", "Artist", 200)


class CountingProvider:
    name = "count"

    def __init__(self, result):
        self.result, self.calls, self.queries = result, 0, []

    def fetch(self, query):
        self.calls += 1
        self.queries.append(query)
        return self.result


SYNCED_RESULT = Lyrics(LyricsStatus.SYNCED, (LyricLine(1, "a"),), source="count")


def test_service_caches_to_disk_and_memoises(tmp_path):
    p = CountingProvider(SYNCED_RESULT)
    svc = LyricsService([p], LyricsCache(tmp_path))
    assert svc.fetch(META).is_synced and svc.fetch(META).is_synced
    assert p.calls == 1 and p.queries[0].title == "Song"
    fresh = CountingProvider(SYNCED_RESULT)
    again = LyricsService([fresh], LyricsCache(tmp_path)).fetch(META)  # new process, same cache
    assert again.is_synced and again.lines[0].text == "a" and fresh.calls == 0
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_service_does_not_cache_failures(tmp_path):
    p = CountingProvider(Lyrics(LyricsStatus.ERROR, message="x"))
    svc = LyricsService([p], LyricsCache(tmp_path))
    svc.fetch(META)
    svc.fetch(META)
    assert p.calls == 2 and not list(tmp_path.glob("*.json"))


def test_service_provider_chain_and_exceptions():
    class Boom:
        name = "boom"

        def fetch(self, query):
            raise RuntimeError("bug")

    second = CountingProvider(SYNCED_RESULT)
    assert LyricsService([Boom(), CountingProvider(Lyrics(LyricsStatus.NOT_FOUND)), second]
                         ).fetch(META).is_synced
    only_plain = CountingProvider(Lyrics(LyricsStatus.UNSYNCED))
    res = LyricsService([CountingProvider(Lyrics(LyricsStatus.NOT_FOUND)), only_plain]).fetch(META)
    assert res.status is LyricsStatus.UNSYNCED
    assert LyricsService([]).fetch(META).status is LyricsStatus.NOT_FOUND
    assert LyricsService([second]).fetch(MediaMetadata("")).status is LyricsStatus.NOT_FOUND


def test_generic_file_names_are_not_searched():
    p = CountingProvider(SYNCED_RESULT)
    for name in ("videoplayback", "VID_0012", "video", "12345", "Screen Recording 3", ""):
        res = LyricsService([p]).fetch(MediaMetadata(name))
        assert res.status is LyricsStatus.NOT_FOUND and "--title" in res.status_text
    assert p.calls == 0
    assert LyricsService([p]).fetch(MediaMetadata("videoplayback", "Band")).status         is LyricsStatus.NOT_FOUND  # artist alone does not identify a track


def test_corrupt_cache_is_ignored(tmp_path):
    cache = LyricsCache(tmp_path)
    query = LyricsQuery.from_metadata(META)
    (tmp_path / f"{query.cache_key}.json").write_text("{not json")
    assert cache.get(query) is None
    json.dumps({})


# ------------------------------------------------------------------ track (async)


def test_lyrics_track_states():
    track = LyricsTrack(lambda: SYNCED_RESULT)
    assert track.message == "Searching for lyrics…" and not track.ready
    track.start()
    assert track.wait(2) and track.is_synced and track.message == ""
    assert track.window_at(1.5).current == ("a",)

    missing = LyricsTrack(lambda: Lyrics(LyricsStatus.NOT_FOUND))
    missing.start()
    missing.wait(2)
    assert not missing.is_synced and missing.message == "No lyrics found."
    assert missing.window_at(5).current == ()

    def explode():
        raise RuntimeError

    broken = LyricsTrack(explode)
    broken.start()
    broken.wait(2)
    assert broken.lyrics.status is LyricsStatus.ERROR

    off = LyricsTrack(None)
    assert off.ready and off.lyrics.status is LyricsStatus.DISABLED


@pytest.mark.parametrize("status", list(LyricsStatus))
def test_every_status_has_text(status):
    assert isinstance(Lyrics(status).status_text, str)
