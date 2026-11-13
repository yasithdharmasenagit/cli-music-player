from __future__ import annotations

import pytest

from src.core.exceptions import (
    InvalidMediaPathError,
    InvalidURLError,
    MediaUnavailableError,
    UnsupportedMediaError,
    VideoLoadError,
)
from src.media.media_resolver import MediaResolver
from src.media.metadata import (
    MediaKind,
    build_metadata,
    clean_title,
    lookup_title,
    split_artist_title,
)
from src.media.youtube_resolver import (
    YouTubeResolver,
    extract_video_id,
    is_youtube_url,
    looks_like_url,
    select_streams,
)

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={VID}",
    f"http://youtube.com/watch?v={VID}&t=30s&list=PLx",
    f"https://youtu.be/{VID}?si=abc",
    f"https://m.youtube.com/watch?v={VID}",
    f"https://music.youtube.com/watch?v={VID}",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/embed/{VID}",
    f"youtube.com/watch?v={VID}",
])
def test_valid_youtube_urls(url):
    assert extract_video_id(url) == VID
    assert is_youtube_url(url)


@pytest.mark.parametrize("url", [
    "", "   ", "https://www.youtube.com/", "https://www.youtube.com/watch",
    "https://www.youtube.com/watch?v=short", "https://www.youtube.com/playlist?list=PL123",
    "https://vimeo.com/12345", "ftp://youtube.com/watch?v=" + VID, "not a url",
    "https://evil.com/?u=youtube.com/watch?v=" + VID,
])
def test_invalid_youtube_urls(url):
    with pytest.raises(InvalidURLError):
        extract_video_id(url)
    assert not is_youtube_url(url)


def test_looks_like_url():
    assert looks_like_url("https://x.y/z") and looks_like_url("youtu.be/abc")
    assert not looks_like_url(r"C:\music\song.mp3")


def test_metadata_cleaning():
    assert clean_title("Song Name (Official Video)") == "Song Name"
    assert clean_title("Song [Lyrics] (HD)") == "Song"
    assert clean_title("Song | Official Music Video") == "Song"
    assert lookup_title("Song (feat. Someone)") == "Song"
    assert split_artist_title("Artist - Title") == ("Artist", "Title")
    assert split_artist_title("Just a title") == (None, "Just a title")


def test_build_metadata_preferences():
    m = build_metadata("Queen - Bohemian Rhapsody (Official Video)", channel="Queen Official",
                       duration=354)
    assert (m.artist, m.title, m.duration) == ("Queen", "Bohemian Rhapsody", 354)
    m = build_metadata("Some Song", channel="Cool Band - Topic")
    assert (m.artist, m.title) == ("Cool Band", "Some Song")
    m = build_metadata("A - B", artist="Explicit Artist")
    assert m.artist == "Explicit Artist"
    m = build_metadata(None, fallback_title="file")
    assert m.title == "file" and m.artist is None
    assert build_metadata("x", duration=-5).duration is None
    assert build_metadata("A - B").display == "A - B"


def fmt(url_, **kw):
    base = {"url": url_, "protocol": "https", "acodec": "none", "vcodec": "none"}
    base.update(kw)
    return base


def test_select_streams_prefers_audio_only_and_near_360p():
    formats = [
        fmt("a_low", acodec="opus", abr=48),
        fmt("a_high", acodec="opus", abr=160),
        fmt("v1080", vcodec="avc1", height=1080),
        fmt("v360", vcodec="avc1", height=360),
        fmt("v480_vp9", vcodec="vp9", height=480),
        fmt("v_drm", vcodec="avc1", height=360, has_drm=True),
        fmt("v_frag", vcodec="avc1", height=360, protocol="http_dash_segments"),
        fmt("v_nourl", vcodec="avc1", height=360, url=None),
    ]
    assert select_streams(formats) == ("a_high", "v360")


def test_select_streams_none_available():
    assert select_streams([]) == (None, None)
    assert select_streams([fmt("v", vcodec="avc1", height=360, has_drm=True)]) == (None, None)


def info(**over):
    data = {"title": "Artist - Song (Official Video)", "channel": "Ch", "duration": 200,
            "formats": [fmt("A", acodec="opus", abr=100), fmt("V", vcodec="avc1", height=360)],
            "http_headers": {"User-Agent": "UA", "Cookie": "secret"}}
    data.update(over)
    return data


def test_resolver_success_and_header_filtering():
    seen = []

    def extractor(url):
        seen.append(url)
        return info()

    media = YouTubeResolver(extractor).resolve(f"https://youtu.be/{VID}")
    assert seen == [f"https://www.youtube.com/watch?v={VID}"]
    assert media.metadata.title == "Song" and media.metadata.artist == "Artist"
    assert media.metadata.duration == 200
    assert media.streams.audio_url == "A" and media.streams.video_url == "V"
    assert "Cookie" not in media.streams.headers  # never forward credentials


def test_resolver_invalid_url_does_not_call_extractor():
    called = []
    with pytest.raises(InvalidURLError):
        YouTubeResolver(lambda u: called.append(u) or {}).resolve("https://vimeo.com/1")
    assert not called


@pytest.mark.parametrize("message,expect", [
    ("Private video. Sign in", "private"),
    ("Video unavailable", "unavailable"),
    ("<urlopen error timed out>", "Network"),
    ("Sign in to confirm your age", "age"),
])
def test_resolver_errors_are_explained_without_leaking(message, expect):
    def boom(_):
        raise RuntimeError(message + " https://googlevideo.com/?sig=SECRET")

    with pytest.raises(MediaUnavailableError) as err:
        YouTubeResolver(boom).resolve(f"https://youtu.be/{VID}")
    assert expect.lower() in str(err.value).lower()
    assert "SECRET" not in str(err.value)


def test_resolver_rejects_live_and_streamless():
    with pytest.raises(MediaUnavailableError, match="Live"):
        YouTubeResolver(lambda u: info(is_live=True)).resolve(f"https://youtu.be/{VID}")
    with pytest.raises(MediaUnavailableError, match="audio stream"):
        YouTubeResolver(lambda u: info(formats=[fmt("V", vcodec="avc1", height=360)])
                        ).resolve(f"https://youtu.be/{VID}")
    with pytest.raises(MediaUnavailableError):
        YouTubeResolver(lambda u: {}).resolve(f"https://youtu.be/{VID}")


def test_resolver_audio_only_when_no_video_format():
    r = YouTubeResolver(lambda u: info(formats=[fmt("A", acodec="opus", abr=1)]))
    assert r.resolve(f"https://youtu.be/{VID}").streams.video_url is None


class FakeYT(YouTubeResolver):
    def __init__(self):
        super().__init__(lambda u: info())


def test_media_resolver_youtube():
    media = MediaResolver(FakeYT(), probe=lambda p: None).resolve(f"https://youtu.be/{VID}")
    assert media.origin == "YouTube" and media.has_video and media.kind is MediaKind.VIDEO
    assert media.audio_source == "A" and media.video_source == "V"


def test_media_resolver_rejects_other_sites():
    with pytest.raises(InvalidURLError):
        MediaResolver(FakeYT(), probe=lambda p: None).resolve("https://vimeo.com/123")


def test_media_resolver_local_video(make_video):
    path = make_video("my_cool_song.avi", frames=10, fps=10)
    media = MediaResolver(FakeYT(), probe=lambda p: None).resolve(f'"{path}"')
    assert media.kind is MediaKind.VIDEO and media.video_source == str(path)
    assert media.metadata.title == "my cool song"
    assert media.metadata.duration == pytest.approx(1.0)


def test_media_resolver_local_audio(tmp_path):
    f = tmp_path / "Artist - Track.mp3"
    f.write_bytes(b"\x00" * 10)
    media = MediaResolver(FakeYT(), probe=lambda p: None).resolve(str(f))
    assert media.kind is MediaKind.AUDIO and media.video_source is None
    assert (media.metadata.artist, media.metadata.title) == ("Artist", "Track")


def test_media_resolver_errors(tmp_path):
    r = MediaResolver(FakeYT(), probe=lambda p: None)
    with pytest.raises(InvalidMediaPathError):
        r.resolve("")
    with pytest.raises(InvalidMediaPathError):
        r.resolve(str(tmp_path / "missing.mp4"))
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    with pytest.raises(UnsupportedMediaError):
        r.resolve(str(txt))
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video" * 50)
    with pytest.raises(VideoLoadError):
        r.resolve(str(broken))
