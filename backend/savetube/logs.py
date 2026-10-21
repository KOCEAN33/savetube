"""Logfmt lines on stderr (docs/plan/operations.md, Logging).

A savetube line is an event with fields: `emit(log, logging.INFO, "job.start",
job=230)`. A library line keeps its logger name and message.
"""

import contextlib
import contextvars
import logging
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any

from savetube.limits import TIMEZONE

_FIELDS = "savetube_fields"
_context: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "savetube_log_context", default=MappingProxyType({})
)

PROGRESS_EVERY = 60
_MAX_VALUE = 300


def _value(v: Any) -> str:
    s = str(v)
    if s and not any(c in s for c in ' "=\\') and s.isprintable():
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    escaped = "".join(c if c.isprintable() or c == " " else repr(c)[1:-1] for c in escaped)
    return f'"{escaped}"'


def _pairs(fields: Mapping[str, Any]) -> list[str]:
    return [f"{k}={_value(v)}" for k, v in fields.items() if v is not None]


class LogfmtFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(int(record.created), TIMEZONE).isoformat()
        parts = [f"ts={ts}", f"level={record.levelname.lower()}"]
        ctx = _context.get()
        fields = getattr(record, _FIELDS, None)
        if fields is not None:
            parts.append(f"event={record.getMessage()}")
            parts += _pairs({**ctx, **fields})
        else:
            parts.append(f"logger={record.name}")
            parts += _pairs(ctx)
            parts.append(f"msg={_value(record.getMessage())}")
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def emit(
    log: logging.Logger, level: int, event: str, /, *, exc_info: bool = False, **fields: Any
) -> None:
    log.log(level, event, extra={_FIELDS: fields}, exc_info=exc_info or None, stacklevel=2)


@contextlib.contextmanager
def context(**fields: Any) -> Iterator[None]:
    """Fields every line in this block carries, the library lines too."""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def current() -> dict[str, Any]:
    """The context fields, for a child process to enter again."""
    return dict(_context.get())


def configure(level: str) -> None:
    """One stderr handler. Libraries log at WARNING, or at INFO when savetube
    logs at DEBUG. Alembic logs at INFO, so a migration shows each revision."""
    savetube_level = logging.getLevelNamesMapping()[level.upper()]
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(LogfmtFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO if savetube_level <= logging.DEBUG else logging.WARNING)
    logging.getLogger("savetube").setLevel(savetube_level)
    logging.getLogger("alembic").setLevel(logging.INFO)


class YtdlpLogger:
    """yt-dlp's `logger` param. Its errors log at DEBUG, because the caller logs
    the same text as `reason=`."""

    _log = logging.getLogger("savetube.ytdlp")

    def debug(self, msg: str) -> None:
        self._log.debug(msg)

    def info(self, msg: str) -> None:
        self._log.debug(msg)

    def warning(self, msg: str) -> None:
        self._log.warning(msg)

    def error(self, msg: str) -> None:
        self._log.debug(msg)


_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(n: float) -> str:
    value, unit = float(n), 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    digits = f"{value:.0f}" if value >= 100 else f"{value:.3g}"
    return f"{digits}{_UNITS[unit]}"


def human_duration(seconds: float) -> str:
    s = int(seconds)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def cut(text: str | None, limit: int = _MAX_VALUE) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def progress(d: dict[str, Any]) -> dict[str, Any]:
    """The current stream's progress from a yt-dlp progress hook dict."""
    done = d.get("downloaded_bytes")
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    pct = None
    if total and done is not None:
        pct = done / total * 100
    elif d.get("fragment_index") and d.get("fragment_count"):
        pct = d["fragment_index"] / d["fragment_count"] * 100
    return {
        "format": (d.get("info_dict") or {}).get("format_id"),
        "pct": round(pct, 1) if pct is not None else None,
        "done": done,
        "total": total,
        "speed": d.get("speed"),
        "eta": d.get("eta"),
    }


class ProgressLog:
    """A progress hook that logs `download.progress` at most once a minute. A
    time bound caps the volume for any file size or rate limit."""

    _log = logging.getLogger("savetube.download")

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: float | None = None

    def __call__(self, d: dict[str, Any]) -> None:
        if d.get("status") != "downloading":
            return
        now = self._clock()
        if self._last is not None and now - self._last < PROGRESS_EVERY:
            return
        self._last = now
        p = progress(d)
        # A live has no total: bytes, fragments and elapsed time show it moves.
        live = p["total"] is None
        emit(
            self._log,
            logging.INFO,
            "download.progress",
            format=p["format"],
            pct=f"{p['pct']:.1f}%" if p["pct"] is not None else None,
            done=human_bytes(p["done"]) if p["done"] is not None else None,
            total=human_bytes(p["total"]) if p["total"] is not None else None,
            speed=f"{human_bytes(p['speed'])}/s" if p["speed"] is not None else None,
            eta=human_duration(p["eta"]) if p["eta"] is not None else None,
            frags=d.get("fragment_index") if live else None,
            elapsed=human_duration(d["elapsed"]) if live and d.get("elapsed") else None,
        )
