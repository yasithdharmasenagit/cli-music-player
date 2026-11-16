from __future__ import annotations

import logging
import sys

import pytest

from src.utils.logger import RedactingFilter, configure_logging, get_logger, redact

SECRET_URL = ("https://rr3---sn-abc.googlevideo.com/videoplayback/extra/more/path"
              "?expire=1&sig=SECRETSIG&key=SECRETKEY&ip=1.2.3.4")


def test_urls_are_reduced_to_host_and_short_path():
    out = redact(f"opening {SECRET_URL} now")
    assert "SECRETSIG" not in out and "SECRETKEY" not in out and "1.2.3.4" not in out
    assert out.startswith("opening https://rr3---sn-abc.googlevideo.com/videoplayback/extra/...")
    assert "?<redacted>" in out and out.endswith(" now")
    assert redact("see https://user:pw@example.com/a") == "see https://example.com/a"
    assert redact("plain http://lrclib.net/api/search") == "plain http://lrclib.net/api/search"


@pytest.mark.parametrize("text", [
    "Cookie: SID=abc123secret", "cookie=abc123secret", "token=abc123secret", "access_token: abc123secret",
    "api_key=abc123secret", "password = abc123secret", "Authorization: Bearer abc123secret",
    "sig=abc123secret&x=1", "set-cookie: abc123secret", "po_token=abc123secret",
])
def test_credentials_are_redacted(text):
    assert "abc123secret" not in redact(text)
    assert "<redacted>" in redact(text)


def test_harmless_text_is_untouched():
    text = "Resolved video 'Song - Artist' (3:21), frames 5326, key frames fine"
    assert redact(text) == text


def test_filter_handles_args_and_exceptions():
    logger = logging.getLogger("test_redact_filter")
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Capture()
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.info("fetching %s with token=%s", SECRET_URL, "abc123secret")
    try:
        raise RuntimeError(f"failed {SECRET_URL}")
    except RuntimeError:
        logger.exception("boom")
    text = " ".join(r.getMessage() + (r.exc_text or "") for r in records)
    assert "SECRETSIG" not in text and "abc123secret" not in text
    assert "googlevideo.com" in text  # still useful for debugging
    logger.removeHandler(handler)


def test_configure_logging_writes_redacted_rotating_file(tmp_path, capsys):
    path = configure_logging(True, "INFO", tmp_path / "logs")
    assert path == tmp_path / "logs" / "player.log"
    log = get_logger("unit")
    log.info("request %s", SECRET_URL)
    log.debug("not written at INFO")
    for h in logging.getLogger("ascii_player").handlers:
        h.flush()
    content = path.read_text(encoding="utf-8")
    assert "request https://rr3---sn-abc.googlevideo.com" in content
    assert "SECRETSIG" not in content and "not written" not in content
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""  # the console stays clean
    handler = next(h for h in logging.getLogger("ascii_player").handlers
                   if h.__class__.__name__ == "RotatingFileHandler")
    assert handler.maxBytes == 1_000_000 and handler.backupCount == 3
    configure_logging(False)


def test_configure_logging_is_idempotent_and_can_be_disabled(tmp_path):
    configure_logging(True, "INFO", tmp_path)
    configure_logging(True, "DEBUG", tmp_path)
    files = [h for h in logging.getLogger("ascii_player").handlers
             if h.__class__.__name__ == "RotatingFileHandler"]
    assert len(files) == 1 and logging.getLogger("ascii_player").level == logging.DEBUG
    assert configure_logging(False, "INFO", tmp_path) is None
    assert not [h for h in logging.getLogger("ascii_player").handlers
                if h.__class__.__name__ == "RotatingFileHandler"]


def test_unwritable_log_dir_disables_file_logging_silently(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert configure_logging(True, "INFO", blocker / "logs") is None
    get_logger("x").warning("still fine")  # no exception, no output
    configure_logging(False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking semantics")
def test_reconfigure_releases_the_log_file(tmp_path):
    configure_logging(True, "INFO", tmp_path)
    configure_logging(False)
    (tmp_path / "player.log").unlink()  # would fail with PermissionError if still open
