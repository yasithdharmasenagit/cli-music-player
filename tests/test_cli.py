"""Application startup, option layering, tool commands and the interactive workflow."""

from __future__ import annotations

import logging

import pytest

from src._version import __version__
from src.cli import main as cli
from src.cli.interface import Interface
from src.core.config import DEFAULT_CONFIG, Quality
from src.core.exceptions import MediaUnavailableError, MpvNotFoundError
from src.player.session import SessionResult
from src.utils.paths import CONFIG_ENV_VAR


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolate every test from the developer's real config, cache and logs."""
    config_path = tmp_path / "cfg" / "config.toml"
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    yield config_path
    logging.getLogger("ascii_player").handlers.clear()


def run(args, capsys):
    code = cli.main(args)
    out = capsys.readouterr()
    return code, out.out + out.err


def parse(args):
    return cli.build_parser().parse_args(args)


# ------------------------------------------------------------------ startup


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_lists_the_documented_options(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = capsys.readouterr().out
    for option in ["--quality", "--width", "--volume", "--no-lyrics", "--title", "--artist",
                   "--doctor", "--init-config", "--show-config", "--config", "--no-audio",
                   "--mpv-path", "--cache-dir", "--no-log", "--ui-fps", "--version"]:
        assert option in text


def test_unknown_option_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--frobnicate"])
    assert exc.value.code == 2


# ------------------------------------------------------------------ layering


def test_cli_options_default_to_none_so_the_file_can_supply_them():
    args = parse([])
    for name in ("quality", "width", "volume", "color", "invert", "show_video", "lyrics_enabled",
                 "ui_fps", "mpv_path", "alt_screen", "cache_dir", "log_level"):
        assert getattr(args, name) is None, name
    assert cli.config_from_args(args) == DEFAULT_CONFIG


def test_precedence_defaults_then_file_then_command_line(env, capsys):
    env.parent.mkdir(parents=True)
    env.write_text('[video]\nquality = "low"\ncolor = true\n[audio]\nvolume = 40\n'
                   '[lyrics]\nenabled = false\n', encoding="utf-8")
    config, warnings, path, exists = cli.load_settings(parse([]))
    assert (config.quality, config.color, config.volume, config.lyrics_enabled) == (
        Quality.LOW, True, 40, False) and exists and path == env
    config, *_ = cli.load_settings(parse(["-q", "high", "--no-color", "--lyrics", "--width", "0"]))
    assert config.quality is Quality.HIGH and not config.color and config.lyrics_enabled
    assert config.volume == 40  # not given on the command line: the file's value stays
    assert config.ascii_width is None  # 0 = automatic


def test_legacy_quality_name_still_accepted():
    assert cli.config_from_args(parse(["-q", "standard"])).quality is Quality.BALANCED


def test_flags_map_to_settings():
    cfg = cli.config_from_args(parse(
        ["--volume", "30", "--invert", "--no-video", "--no-alt-screen", "--no-log",
         "--log-level", "debug", "--mpv-path", "x.exe", "--ui-fps", "10", "--charset", " .#",
         "--no-stats", "--no-drop", "--fps", "12", "--cache-dir", "somewhere"]))
    assert (cfg.volume, cfg.invert, cfg.show_video, cfg.alt_screen) == (30, True, False, False)
    assert (cfg.log_enabled, cfg.log_level, cfg.mpv_path, cfg.ui_fps) == (False, "DEBUG", "x.exe", 10)
    assert cfg.charset == " .#" and not cfg.show_stats and not cfg.drop_frames and cfg.fps == 12
    assert str(cfg.cache_dir) == "somewhere"


# ------------------------------------------------------------------ bad configuration


def test_invalid_config_file_exits_2_naming_the_key(env, capsys):
    env.parent.mkdir(parents=True)
    env.write_text("[audio]\nvolume = 999\n", encoding="utf-8")
    code, out = run([], capsys)
    assert code == 2 and "[audio] volume" in out and "Traceback" not in out


def test_missing_explicit_config_file_exits_2(env, capsys, tmp_path):
    code, out = run(["--config", str(tmp_path / "nope.toml"), "--doctor"], capsys)
    assert code == 2 and "not found" in out


def test_invalid_command_line_value_exits_2(env, capsys):
    code, out = run(["--volume", "500"], capsys)
    assert code == 2 and "volume" in out


def test_unknown_config_key_warns_and_continues(env, capsys):
    env.parent.mkdir(parents=True)
    env.write_text('[video]\nquallity = "low"\n', encoding="utf-8")
    code, out = run(["--show-config"], capsys)
    assert code == 0 and "quallity" in out


# ------------------------------------------------------------------ tool commands


def test_init_config_creates_but_never_overwrites(env, capsys):
    code, out = run(["--init-config"], capsys)
    assert code == 0 and env.exists() and "Created" in out
    env.write_text("# mine\n", encoding="utf-8")
    code, out = run(["--init-config"], capsys)
    assert code == 0 and "Already exists" in out and env.read_text() == "# mine\n"


def test_show_config_prints_effective_settings(env, capsys):
    code, out = run(["--show-config", "-q", "low", "--volume", "33"], capsys)
    assert code == 0
    for needle in ["quality", "low", "volume", "33", "cache folder", "not created"]:
        assert needle in out


def test_doctor_passes_when_required_tools_exist(env, capsys, monkeypatch):
    monkeypatch.setattr("src.audio.audio_player.find_mpv", lambda configured=None: "mpv-fake.exe")
    code, out = run(["--doctor"], capsys)
    assert code == 0 and "Everything required is in place" in out
    for needle in ["Python", "OpenCV", "NumPy", "Rich", "httpx", "mpv", "FFmpeg", "Config file"]:
        assert needle in out


def test_doctor_fails_with_install_hint_when_mpv_is_missing(env, capsys, monkeypatch):
    def missing(configured=None):
        raise MpvNotFoundError("nope")

    monkeypatch.setattr("src.audio.audio_player.find_mpv", missing)
    code, out = run(["--doctor"], capsys)
    assert code == 1 and "winget install" in out and "required check(s) failed" in out


# ------------------------------------------------------------------ playing


def fake_result(**kw):
    return SessionResult(frames_delivered=10, frames_skipped=1, **kw)


def test_successful_playback_exits_0_and_writes_a_log(env, capsys, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "play_media", lambda raw, cfg, ui, title=None, artist=None:
                        calls.append((raw, cfg, title, artist)) or fake_result(quit_by_user=True))
    code, out = run(["song.mp3", "--title", "T", "--artist", "A", "-q", "low"], capsys)
    assert code == 0 and "Playback stopped" in out and "10 video frames shown" in out
    raw, cfg, title, artist = calls[0]
    assert (raw, title, artist, cfg.quality) == ("song.mp3", "T", "A", Quality.LOW)
    log = tmp_path / "local" / "ascii-music-player" / "cache" / "logs" / "player.log"
    assert log.exists() and f"ascii-music-player {__version__}" in log.read_text(encoding="utf-8")


def test_no_log_flag_writes_nothing(env, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "play_media", lambda *a, **k: fake_result())
    run(["x.mp3", "--no-log"], capsys)
    assert not list(tmp_path.rglob("player.log"))


def test_session_error_is_reported_without_traceback(env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "play_media", lambda *a, **k: fake_result(error="Audio playback failed: boom"))
    code, out = run(["x.mp3"], capsys)
    assert code == 1 and "Audio playback failed: boom" in out and "Traceback" not in out


def test_stall_and_recovery_diagnostics_are_shown(env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "play_media", lambda *a, **k: fake_result(
        stalls=2, max_stall=1.5, max_render_ms=900, video_recoveries=3, notice="Video could not be decoded"))
    code, out = run(["x.mp3"], capsys)
    assert code == 0
    for needle in ["stalled 2x", "-q low", "dropped 3x", "Video could not be decoded"]:
        assert needle in out


def test_missing_mpv_is_a_clean_error(env, capsys, monkeypatch):
    def missing(configured=None):
        raise MpvNotFoundError("mpv was not found. Install it")

    monkeypatch.setattr(cli, "find_mpv", missing)
    code, out = run(["x.mp3"], capsys)
    assert code == 1 and "mpv was not found" in out and "--doctor" in out and "Traceback" not in out


def test_invalid_url_is_a_clean_error(env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "find_mpv", lambda configured=None: "mpv-fake")
    code, out = run(["https://vimeo.com/123"], capsys)
    assert code == 1 and "Unsupported site" in out and "youtube.com/watch" in out


def test_missing_file_is_a_clean_error(env, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "find_mpv", lambda configured=None: "mpv-fake")
    code, out = run([str(tmp_path / "nope.mp4")], capsys)
    assert code == 1 and "File not found" in out


def test_unavailable_stream_offers_local_playback(env, capsys, monkeypatch):
    def unavailable(*a, **k):
        raise MediaUnavailableError("This video is private.")

    monkeypatch.setattr(cli, "play_media", unavailable)
    code, out = run(["https://www.youtube.com/watch?v=dQw4w9WgXcQ"], capsys)
    assert code == 1 and "private" in out and "local" in out


def test_ctrl_c_exits_130(env, capsys, monkeypatch):
    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "play_media", interrupted)
    assert cli.main(["x.mp3"]) == 130


def test_unexpected_exception_never_shows_a_traceback(env, capsys, monkeypatch):
    def boom(*a, **k):
        raise ZeroDivisionError("oops")

    monkeypatch.setattr(cli, "play_media", boom)
    code, out = run(["x.mp3"], capsys)
    assert code == 1 and "Unexpected error: ZeroDivisionError" in out and "Traceback" not in out
    log = logging.getLogger("ascii_player")
    assert log is not None


def test_silent_mode_plays_a_real_video(env, capsys, make_video):
    path = make_video(frames=10, fps=10.0)
    code, out = run([str(path), "--no-audio", "--fps", "500", "--no-alt-screen", "-q", "low"], capsys)
    assert code == 0 and "Playback finished" in out


# ------------------------------------------------------------------ interactive workflow


def script_prompts(monkeypatch, answers):
    queue = list(answers)
    prompts = []

    def prompt(self):
        prompts.append(1)
        return queue.pop(0)

    monkeypatch.setattr(Interface, "prompt_media_path", prompt)
    return prompts


def test_interactive_session_handles_repeated_requests(env, capsys, monkeypatch):
    prompts = script_prompts(monkeypatch, ["first.mp3", "second.mp3", "third.mp3", None])
    seen = []

    def play(raw, cfg, ui, title=None, artist=None):
        seen.append(raw)
        if raw == "second.mp3":
            raise MediaUnavailableError("nope")  # a failure must not end the interactive session
        return fake_result()

    monkeypatch.setattr(cli, "play_media", play)
    code, out = run([], capsys)
    assert code == 0 and seen == ["first.mp3", "second.mp3", "third.mp3"] and len(prompts) == 4
    assert out.count("Playback finished") == 2 and "nope" in out


def test_interactive_quit_immediately(env, capsys, monkeypatch):
    script_prompts(monkeypatch, [None])
    monkeypatch.setattr(cli, "play_media", lambda *a, **k: pytest.fail("must not play"))
    code, out = run([], capsys)
    assert code == 0 and "ASCII MUSIC PLAYER" in out and "Watch music. Differently." in out


def test_each_play_request_builds_fresh_components(env, capsys, monkeypatch, make_video):
    """play_media must not share mpv/clock/session state between requests."""
    from tests.test_session_integration import FakeMpv  # reuse the fake audio engine

    monkeypatch.setattr(cli, "find_mpv", lambda configured=None: "mpv-fake")
    made = []

    class Backend(FakeMpv):
        def __init__(self, clock, mpv_path=None):
            super().__init__(clock, 0.5)
            made.append(self)

    monkeypatch.setattr(cli, "MpvBackend", Backend)
    path = make_video(frames=10, fps=20.0)
    ui = Interface()
    cfg = DEFAULT_CONFIG.with_overrides(alt_screen=False, lyrics_enabled=False)
    first = cli.play_media(str(path), cfg, ui)
    second = cli.play_media(str(path), cfg, ui)
    assert first.completed and second.completed
    assert len(made) == 2 and made[0] is not made[1] and made[0].stopped and made[1].stopped
