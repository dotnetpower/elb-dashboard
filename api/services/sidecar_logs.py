"""Sidecar log tailing for the Live Wall monitor.

Responsibility: Resolve local sidecar log files, redact sensitive substrings, and
  return bounded recent tails for Live Wall routes.
Edit boundaries: Keep HTTP tickets and SSE response shaping in routes; this
  module only reads local log files and normalises log-line payloads.
Key entry points: `SIDECAR_CONTAINERS`, `read_recent_lines`, `read_lines_since`,
  `log_path_for`
Risky contracts: Never expose bearer tokens, Authorization headers, SAS
  signatures, or URL credentials read from raw process logs.
Validation: `uv run pytest -q api/tests/test_sidecar_logs.py`.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

SidecarContainer = Literal["frontend", "api", "worker", "beat", "redis", "terminal"]
LogLevel = Literal["DBG", "INFO", "WARN", "ERR", "OK"]
LogStream = Literal["stdout", "stderr"]


class LogLine(TypedDict):
    ts: str
    stream: LogStream
    text: str
    level: NotRequired[LogLevel]


SIDECAR_CONTAINERS: tuple[SidecarContainer, ...] = (
    "frontend",
    "api",
    "worker",
    "beat",
    "redis",
    "terminal",
)

_LOG_FILE_BY_CONTAINER: dict[SidecarContainer, str] = {
    "frontend": "web.log",
    "api": "api.log",
    "worker": "worker.log",
    "beat": "beat.log",
    "redis": "redis.log",
    "terminal": "terminal-exec.log",
}

_MAX_READ_BYTES = 512 * 1024
_MAX_LINE_CHARS = 4_000
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]{16,})"),
    re.compile(r"(?i)\b(basic\s+)([A-Za-z0-9+/=]{16,})"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)([?&](?:sig|se|sp|skoid|sktid|skt|ske|sks|skv)=)([^\s&]+)"),
    re.compile(r"(?i)\b(https?://[^\s/:@]+):([^\s/@]+)@"),
    re.compile(r"(?i)((?:password|passwd|pwd|secret|token)\s*[:=]\s*)['\"]?([^\s'\"&]{8,})"),
)


def log_path_for(container: SidecarContainer, *, log_base: Path | None = None) -> Path:
    """Return the local-run log file path for a Live Wall sidecar."""
    base = log_base or _default_log_base()
    return base / "latest" / _LOG_FILE_BY_CONTAINER[container]


def read_recent_lines(
    container: SidecarContainer,
    *,
    tail: int = 200,
    log_base: Path | None = None,
) -> list[LogLine]:
    """Return the latest sanitized lines for one sidecar.

    Missing logs are normal in partial local sessions, so this returns an empty
    list instead of raising.
    """
    path = log_path_for(container, log_base=log_base)
    raw_lines = _tail_file(path, max(1, min(2_000, tail)))
    return [_to_log_line(raw) for raw in raw_lines]


def read_lines_since(
    container: SidecarContainer,
    offset: int,
    *,
    log_base: Path | None = None,
) -> tuple[list[LogLine], int]:
    """Return sanitized log lines appended after byte `offset`.

    If the file was rotated or truncated, reading resumes from byte zero.
    """
    path = log_path_for(container, log_base=log_base)
    if not path.exists() or not path.is_file():
        return [], 0
    size = path.stat().st_size
    start = offset if 0 <= offset <= size else 0
    with path.open("rb") as handle:
        handle.seek(start)
        payload = handle.read(_MAX_READ_BYTES)
        new_offset = handle.tell()
    if not payload:
        return [], size
    text = payload.decode("utf-8", errors="replace")
    return [_to_log_line(line) for line in text.splitlines() if line.strip()], new_offset


def end_offset(container: SidecarContainer, *, log_base: Path | None = None) -> int:
    """Return the current byte length of a sidecar log file."""
    path = log_path_for(container, log_base=log_base)
    if not path.exists() or not path.is_file():
        return 0
    return path.stat().st_size


def _default_log_base() -> Path:
    override = os.environ.get("LOCAL_LOG_BASE")
    if override:
        return Path(override)
    return _PROJECT_ROOT / ".logs" / "local"


def _tail_file(path: Path, tail: int) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    size = path.stat().st_size
    start = max(0, size - _MAX_READ_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        payload = handle.read()
    lines = payload.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines[-tail:]


def _to_log_line(raw: str) -> LogLine:
    text = _mask_secrets(raw.strip())[:_MAX_LINE_CHARS]
    level = _infer_level(text)
    stream: LogStream = "stderr" if level in {"WARN", "ERR"} else "stdout"
    payload: LogLine = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "stream": stream,
        "text": text,
    }
    if level is not None:
        payload["level"] = level
    return payload


def _infer_level(text: str) -> LogLevel:
    lower = text.lower()
    if any(marker in lower for marker in ("traceback", "exception", " error", "failed", " err")):
        return "ERR"
    if any(marker in lower for marker in ("warning", " warn", "degraded", "retry")):
        return "WARN"
    if any(marker in lower for marker in ("succeeded", "ready", "healthy", " 200 ", " ok")):
        return "OK"
    if "debug" in lower:
        return "DBG"
    return "INFO"


def _mask_secrets(line: str) -> str:
    out = line
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_redact_match, out)
    return out


def _redact_match(match: re.Match[str]) -> str:
    if match.lastindex and match.lastindex >= 2:
        return f"{match.group(1)}***REDACTED***"
    return cast(str, "***REDACTED***")
