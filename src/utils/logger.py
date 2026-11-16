"""Application logging: rotating file log with automatic redaction of sensitive data.

Nothing is ever logged to the console (that would corrupt the full-screen player); normal CLI
output stays clean and diagnostics go to ``<cache>/logs/player.log``.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

_ROOT_NAME = "ascii_player"
_URL = re.compile(r"https?://[^\s'\"<>)\]]+", re.IGNORECASE)
_SECRET_KV = re.compile(
    r"(?i)\b(token|access[_-]?token|sig|signature|sign|key|api[_-]?key|secret|password|passwd|"
    r"pwd|authorization|cookie|set-cookie|session|sessionid|sapisid|visitor[_-]?data|po[_-]?token)"
    r"\b(\s*[=:]\s*)([^\s&;,'\"]+)")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")


def _shorten_url(match: re.Match[str]) -> str:
    """Keep scheme, host and at most two path segments; drop query, fragment, userinfo."""
    raw = match.group(0)
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
    except ValueError:
        return "<url>"
    segments = [s for s in parts.path.split("/") if s]
    path = "/" + "/".join(segments[:2]) if segments else ""
    more = "/..." if len(segments) > 2 else ""
    query = "?<redacted>" if parts.query else ""
    return f"{parts.scheme}://{host}{path}{more}{query}"


def redact(text: str) -> str:
    """Remove URLs' query strings/tokens, credentials and bearer tokens from *text*."""
    text = _URL.sub(_shorten_url, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} <redacted>", text)
    return _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)


class RedactingFilter(logging.Filter):
    """Applies :func:`redact` to every record (message and string arguments)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format string: log it as-is rather than lose it
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info:
            # Tracebacks may embed URLs; render and redact them too.
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a namespaced logger."""
    return logging.getLogger(f"{_ROOT_NAME}.{name}" if name else _ROOT_NAME)


def configure_logging(enabled: bool = True, level: str | int = "INFO",
                      log_dir: Path | None = None) -> Path | None:
    """(Re)configure the application log. Returns the log file path, or ``None`` if disabled.

    Safe to call repeatedly: handlers installed by earlier calls are replaced. Failure to
    create the log file (read-only disk, permissions) disables file logging silently.
    """
    root = logging.getLogger(_ROOT_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.propagate = False
    root.setLevel(level if isinstance(level, int) else str(level).upper())
    root.addHandler(logging.NullHandler())
    if not enabled or log_dir is None:
        return None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "player.log"
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    return path
