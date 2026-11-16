from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import (
    CHARSET_BALANCED,
    CHARSET_HIGH,
    CHARSET_STANDARD,
    DEFAULT_CONFIG,
    AppConfig,
    Quality,
)
from src.core.exceptions import ConfigError
from src.core.user_config import (
    EXAMPLE_CONFIG,
    describe_config,
    load_config_file,
    parse_config_text,
    write_example_config,
)
from src.utils.paths import CONFIG_ENV_VAR, default_cache_dir, default_config_path

# ------------------------------------------------------------------ quality profiles


def test_three_profiles_with_increasing_detail():
    low, balanced, high = Quality.LOW.profile, Quality.BALANCED.profile, Quality.HIGH.profile
    assert len(low.charset) < len(balanced.charset) < len(high.charset)
    assert low.max_width < balanced.max_width < high.max_width
    assert low.ui_fps < balanced.ui_fps
    assert low.interpolation == "linear" and balanced.interpolation == high.interpolation == "area"
    assert low.charset == CHARSET_STANDARD and high.charset == CHARSET_HIGH
    assert balanced.charset == CHARSET_BALANCED and len(CHARSET_BALANCED) == 24


def test_balanced_ramp_is_a_darkness_ordered_subset_of_high():
    positions = [CHARSET_HIGH.index(c) for c in CHARSET_BALANCED]
    assert positions == sorted(positions)
    assert CHARSET_BALANCED[0] == " " and CHARSET_BALANCED[-1] == CHARSET_HIGH[-1]


def test_quality_parse_and_legacy_alias():
    assert Quality.parse("LOW") is Quality.LOW and Quality.parse(" high ") is Quality.HIGH
    assert Quality.parse("standard") is Quality.BALANCED  # legacy name from earlier versions
    assert Quality.parse("medium") is Quality.BALANCED
    assert [q.label for q in Quality] == ["low", "medium", "high"]
    with pytest.raises(ConfigError, match="low, medium, high"):
        Quality.parse("ultra")


def test_quality_defaults_flow_into_config():
    assert DEFAULT_CONFIG.quality is Quality.BALANCED
    for q in Quality:
        cfg = AppConfig(quality=q)
        assert cfg.effective_width == q.profile.max_width
        assert cfg.effective_ui_fps == q.profile.ui_fps
        assert cfg.active_charset == q.charset
        assert cfg.interpolation == q.profile.interpolation
    custom = AppConfig(quality=Quality.LOW, ascii_width=100, ui_fps=30, charset="ab")
    assert (custom.effective_width, custom.effective_ui_fps, custom.active_charset) == (100, 30, "ab")


# ------------------------------------------------------------------ AppConfig validation


@pytest.mark.parametrize("kwargs", [
    {"ascii_width": 5}, {"ascii_width": 501}, {"char_aspect": 0}, {"char_aspect": float("nan")},
    {"fps": 0}, {"fps": -3}, {"volume": -1}, {"volume": 131}, {"volume_step": 0},
    {"seek_step": 0.1}, {"ui_fps": 0.5}, {"ui_fps": 500}, {"lyrics_timeout": 0},
    {"lyrics_cache_max_entries": -1}, {"charset": "x"}, {"log_level": "LOUD"},
    {"lyrics_api_url": "ftp://example.com"},
])
def test_invalid_settings_are_rejected(kwargs):
    with pytest.raises(ConfigError):
        AppConfig(**kwargs)


def test_with_overrides_validates():
    cfg = DEFAULT_CONFIG.with_overrides(volume=50)
    assert cfg.volume == 50 and DEFAULT_CONFIG.volume == 80  # immutable
    with pytest.raises(ConfigError):
        DEFAULT_CONFIG.with_overrides(volume=999)


# ------------------------------------------------------------------ config file


def test_example_config_parses_to_the_defaults():
    cfg, warnings = parse_config_text(EXAMPLE_CONFIG)
    assert warnings == []
    assert cfg == DEFAULT_CONFIG


def test_config_values_are_applied(tmp_path):
    text = f"""
[video]
quality = "high"
width = 90
color = true
charset = " .#"
[audio]
volume = 55
seek_step = 10
mpv_path = "C:/tools/mpv.exe"
[lyrics]
enabled = false
timeout = 8
[ui]
show_video = false
fps = 12
[paths]
cache_dir = "{(tmp_path / 'c').as_posix()}"
[logging]
level = "debug"
enabled = false
"""
    cfg, warnings = parse_config_text(text)
    assert warnings == []
    assert cfg.quality is Quality.HIGH and cfg.ascii_width == 90 and cfg.color
    assert cfg.charset == " .#" and cfg.volume == 55 and cfg.seek_step == 10
    assert cfg.mpv_path == "C:/tools/mpv.exe" and not cfg.lyrics_enabled
    assert cfg.lyrics_timeout == 8 and not cfg.show_video and cfg.ui_fps == 12
    assert cfg.cache_dir == tmp_path / "c" and cfg.log_level == "DEBUG" and not cfg.log_enabled


def test_zero_and_empty_mean_automatic():
    cfg, _ = parse_config_text('[video]\nwidth = 0\ncharset = ""\n[ui]\nfps = 0\n[audio]\nmpv_path = ""\n'
                               '[paths]\ncache_dir = ""\n')
    assert cfg.ascii_width is None and cfg.ui_fps is None and cfg.charset is None
    assert cfg.mpv_path is None and cfg.cache_dir is None


def test_unknown_keys_warn_but_do_not_fail():
    cfg, warnings = parse_config_text('[video]\nquallity = "low"\n[sound]\nx = 1\n')
    assert cfg == DEFAULT_CONFIG
    assert any("quallity" in w for w in warnings) and any("[sound]" in w for w in warnings)


@pytest.mark.parametrize("text,fragment", [
    ('[video]\nquality = "ultra"', "[video] quality"),
    ('[video]\nquality = 3', "[video] quality"),
    ('[video]\ncolor = "yes"', "[video] color"),
    ('[audio]\nvolume = 200', "[audio] volume"),
    ('[audio]\nvolume = "loud"', "[audio] volume"),
    ('[audio]\nvolume = 80.5', "[audio] volume"),
    ('[audio]\nvolume = true', "[audio] volume"),
    ('[lyrics]\ntimeout = 0', "[lyrics] timeout"),
    ('[lyrics]\napi_url = "ftp://x"', "api_url"),
    ('[logging]\nlevel = "LOUD"', "[logging] level"),
    ('[video]\nwidth = 5', "width"),
    ('video = 3', "must be a table"),
    ('[video\nquality = "low"', "not valid TOML"),
])
def test_invalid_config_names_the_offending_key(text, fragment):
    with pytest.raises(ConfigError) as err:
        parse_config_text(text, source="my.toml")
    assert fragment in str(err.value)


def test_load_config_file_cases(tmp_path):
    missing = tmp_path / "nope.toml"
    assert load_config_file(missing) == (DEFAULT_CONFIG, [])  # default location: absence is fine
    with pytest.raises(ConfigError, match="not found"):
        load_config_file(missing, must_exist=True)
    f = tmp_path / "c.toml"
    f.write_bytes(b"\xef\xbb\xbf[audio]\nvolume = 10\n")  # Notepad adds a UTF-8 BOM
    assert load_config_file(f)[0].volume == 10
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config_file(bad)
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config_file(tmp_path)  # a directory


def test_write_example_config_is_safe(tmp_path):
    path = tmp_path / "sub" / "config.toml"
    assert write_example_config(path) is True and path.read_text(encoding="utf-8") == EXAMPLE_CONFIG
    path.write_text("# mine", encoding="utf-8")
    assert write_example_config(path) is False and path.read_text() == "# mine"  # never clobbers
    assert write_example_config(path, overwrite=True) is True
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(ConfigError, match="Cannot write"):
        write_example_config(blocker / "config.toml")


def test_repo_example_file_matches_builtin_example():
    repo_example = Path(__file__).resolve().parents[1] / "config.example.toml"
    assert repo_example.read_text(encoding="utf-8").replace("\r\n", "\n") == EXAMPLE_CONFIG


def test_describe_config_rows(tmp_path):
    rows = dict(describe_config(DEFAULT_CONFIG, tmp_path / "c.toml", False, tmp_path))
    assert "not created" in rows["config file"] and "balanced" in rows["quality"]
    assert rows["cache folder"] == str(tmp_path)


# ------------------------------------------------------------------ locations


def test_default_locations_per_platform():
    env = {"APPDATA": r"C:\Users\u\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    assert default_config_path(env, "win32") == Path(env["APPDATA"]) / "ascii-music-player" / "config.toml"
    assert default_cache_dir(env, "win32") == Path(env["LOCALAPPDATA"]) / "ascii-music-player" / "cache"
    xdg = {"XDG_CONFIG_HOME": "/x/cfg", "XDG_CACHE_HOME": "/x/cache"}
    assert default_config_path(xdg, "linux") == Path("/x/cfg/ascii-music-player/config.toml")
    assert default_cache_dir(xdg, "linux") == Path("/x/cache/ascii-music-player")
    override = {CONFIG_ENV_VAR: "~/my.toml", "APPDATA": "ignored"}
    assert default_config_path(override, "win32") == Path("~/my.toml").expanduser()
