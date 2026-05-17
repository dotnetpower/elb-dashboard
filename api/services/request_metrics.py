"""Process-local request metrics ring buffer.

Tracks one sample per HTTP request handled by the api sidecar so the SPA
can render p50/p95/p99 latency, error rate, and a per-minute throughput
sparkline without depending on an external time-series store.

Bounded memory: a single deque with `maxlen=DEFAULT_CAPACITY`, oldest
sample evicted FIFO. Thread-safe via a single coarse lock — recording
is microseconds, summarisation iterates a snapshot under the lock.

This metrics view is per-process. When the api sidecar is replicated or
restarted, the buffer resets. That is intentional — a real time-series
backend is out of scope (no managed DB), and the dashboard treats
`degraded_reason: "no_samples"` as "data not available yet".

Path normalisation strips path parameters that would otherwise blow up
the cardinality of `by_path` aggregates (job id, cluster name, etc.).
The substitutions are conservative: only well-known prefixes get
collapsed, anything else keeps its raw shape so we don't accidentally
hide a legitimately distinct route.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

DEFAULT_CAPACITY = 8192
MAX_WINDOW_SECONDS = 24 * 60 * 60  # never report beyond 24h
# Hard cap on the length of a path stored in the buffer.  The middleware
# records the *raw* incoming path before normalisation, so an attacker
# can fuzz arbitrarily long URLs (FastAPI returns 404 but middleware
# still fires).  Capping here keeps both per-sample memory and the
# `by_path` aggregate size bounded.
MAX_PATH_LEN = 256

# --- Per-request DETAIL ring buffer (HTTP inspector) -----------------------
# A separate, smaller ring buffer that records full headers + (capped) body
# for the most recent N requests so the SPA can render the per-request HTTP
# inspector ("View HTTP requests" button on the SidecarsCard). Kept small
# because each sample is ~10-30 KiB versus ~80 B for an aggregate sample.
DETAIL_CAPACITY_DEFAULT = 256
# 4 KiB per body — covers most JSON request/response payloads. Anything
# larger is replaced with a "<truncated …>" sentinel.
DETAIL_BODY_CAP_BYTES = 4 * 1024
# Header values that are always replaced with the redaction sentinel.
# Lower-cased on capture for case-insensitive matching.
DETAIL_REDACT_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-functions-key",
        "x-ms-client-secret",
    }
)
DETAIL_REDACT_PLACEHOLDER = "********** (redacted)"
# Content-Type prefixes whose body we are willing to capture. Anything else
# (binary, multipart upload, octet-stream …) is captured as a marker only.
DETAIL_CAPTURABLE_TYPES: tuple[str, ...] = (
    "application/json",
    "application/x-www-form-urlencoded",
    "text/",
)


def _capacity() -> int:
    raw = os.environ.get("REQUEST_METRICS_CAPACITY", "")
    if raw.isdigit():
        n = int(raw)
        if 256 <= n <= 1_000_000:
            return n
    return DEFAULT_CAPACITY


@dataclass(frozen=True, slots=True)
class _Sample:
    ts: float  # epoch seconds (monotonic-anchored wall clock — see record())
    path: str  # normalised request path
    status: int  # HTTP status code (0 if dispatch raised)
    duration_ms: float  # wall-clock latency in milliseconds


# Path collapsing: well-known UUID/job-id/cluster patterns -> {placeholder}.
_PATH_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/api/blast/jobs/[^/]+"), "/api/blast/jobs/{id}"),
    (re.compile(r"/api/blast/databases/[^/]+/shard"), "/api/blast/databases/{db}/shard"),
    (re.compile(r"/api/blast/databases/[^/]+"), "/api/blast/databases/{db}"),
    (re.compile(r"/api/aks/[^/]+/assign-roles"), "/api/aks/{cluster}/assign-roles"),
    (re.compile(r"/api/aks/openapi/deploy/[^/]+/status"), "/api/aks/openapi/deploy/{id}/status"),
    (re.compile(r"/api/blast/submit/[^/]+/status"), "/api/blast/submit/{id}/status"),
    (re.compile(r"/api/tasks/[^/]+"), "/api/tasks/{id}"),
    (re.compile(r"/api/terminal/[^/]+/.*"), "/api/terminal/{vm}/{rest}"),
)


def normalise_path(raw: str) -> str:
    """Collapse path parameters to keep `by_path` cardinality bounded.

    Also caps the returned path at ``MAX_PATH_LEN`` characters so
    arbitrary fuzz traffic cannot bloat per-sample memory.  Truncated
    paths get a `…` sentinel so they're visibly distinct from genuine
    routes in the dashboard.
    """
    if not raw:
        return ""
    # Drop query string (defensive — middleware should already strip it).
    if "?" in raw:
        raw = raw.split("?", 1)[0]
    # Collapse trailing slashes (FastAPI keeps both shapes alive).
    if len(raw) > 1 and raw.endswith("/"):
        raw = raw[:-1]
    for pat, repl in _PATH_RULES:
        if pat.match(raw):
            return repl
    if len(raw) > MAX_PATH_LEN:
        return raw[: MAX_PATH_LEN - 1] + "…"
    return raw


class _RequestMetrics:
    def __init__(self, capacity: int) -> None:
        self._buf: deque[_Sample] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._capacity = capacity

    @property
    def capacity(self) -> int:
        return self._capacity

    def record(
        self,
        *,
        path: str,
        status: int,
        duration_ms: float,
        ts: float | None = None,
    ) -> None:
        sample = _Sample(
            ts=float(ts if ts is not None else time.time()),
            path=normalise_path(path),
            status=int(status),
            duration_ms=max(0.0, float(duration_ms)),
        )
        with self._lock:
            self._buf.append(sample)

    def _snapshot_window(
        self,
        *,
        now: float,
        window: float,
        path_prefix: str | None,
    ) -> list[_Sample]:
        cutoff = now - window
        with self._lock:
            # Snapshot under lock; iterate outside.
            samples = list(self._buf)
        if path_prefix:
            return [s for s in samples if s.ts >= cutoff and s.path.startswith(path_prefix)]
        return [s for s in samples if s.ts >= cutoff]

    def summarise(
        self,
        *,
        window_seconds: int,
        path_prefix: str | None = None,
        rpm_buckets: int = 60,
    ) -> dict[str, object]:
        """Return aggregate stats over the most recent `window_seconds`.

        `rpm_buckets`: how many one-minute buckets to return (most recent
        last). Capped at `window_seconds // 60` so we never invent zero
        buckets that pre-date the window.
        """
        if window_seconds <= 0 or window_seconds > MAX_WINDOW_SECONDS:
            raise ValueError(f"window_seconds must be in (0, {MAX_WINDOW_SECONDS}]")
        rpm_buckets = max(1, min(int(rpm_buckets), max(1, window_seconds // 60)))
        now = time.time()
        window = float(window_seconds)
        samples = self._snapshot_window(now=now, window=window, path_prefix=path_prefix)

        total = len(samples)
        if not total:
            return {
                "window_seconds": window_seconds,
                "now_ts": now,
                "path_prefix": path_prefix,
                "total": 0,
                "errors": 0,
                "error_rate": 0.0,
                "p50_ms": None,
                "p95_ms": None,
                "p99_ms": None,
                "rpm": _empty_rpm(now=now, buckets=rpm_buckets),
                "by_path": [],
                "degraded": True,
                "degraded_reason": "no_samples",
            }

        durations = sorted(s.duration_ms for s in samples)
        errors = sum(1 for s in samples if s.status >= 500 or s.status == 0)

        # Per-minute counts. Buckets index 0 = oldest.
        bucket_size = 60.0
        oldest = now - rpm_buckets * bucket_size
        counts = [0] * rpm_buckets
        for s in samples:
            if s.ts < oldest:
                continue
            idx = int((s.ts - oldest) // bucket_size)
            if 0 <= idx < rpm_buckets:
                counts[idx] += 1
        rpm = [
            {
                "t_end": oldest + (i + 1) * bucket_size,
                "count": counts[i],
            }
            for i in range(rpm_buckets)
        ]

        # Per-path breakdown (top 8 by volume).
        per_path: dict[str, list[float]] = {}
        per_path_errors: dict[str, int] = {}
        for s in samples:
            per_path.setdefault(s.path, []).append(s.duration_ms)
            if s.status >= 500 or s.status == 0:
                per_path_errors[s.path] = per_path_errors.get(s.path, 0) + 1
        top_paths = sorted(per_path.items(), key=lambda kv: -len(kv[1]))[:8]
        by_path = [
            {
                "path": p,
                "count": len(d),
                "errors": per_path_errors.get(p, 0),
                "p95_ms": _percentile(sorted(d), 0.95),
            }
            for p, d in top_paths
        ]

        return {
            "window_seconds": window_seconds,
            "now_ts": now,
            "path_prefix": path_prefix,
            "total": total,
            "errors": errors,
            "error_rate": (errors / total) if total else 0.0,
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
            "p99_ms": _percentile(durations, 0.99),
            "rpm": rpm,
            "by_path": by_path,
        }


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    if not 0.0 < q < 1.0:
        raise ValueError("q must be in (0, 1)")
    # Nearest-rank — sufficient for dashboard display.
    idx = max(0, min(len(sorted_values) - 1, int(round(q * len(sorted_values)) - 1)))
    return round(sorted_values[idx], 2)


def _empty_rpm(*, now: float, buckets: int) -> list[dict[str, object]]:
    bucket_size = 60.0
    oldest = now - buckets * bucket_size
    return [{"t_end": oldest + (i + 1) * bucket_size, "count": 0} for i in range(buckets)]


# Module-level singleton.  __init__ is cheap so we instantiate eagerly.
_METRICS = _RequestMetrics(capacity=_capacity())


def metrics() -> _RequestMetrics:
    return _METRICS


# Convenience for tests.
def reset_for_tests() -> None:
    """Clear the buffer. ONLY for unit tests. NOT thread-safe with concurrent
    recorders, which is fine because tests don't run uvicorn in parallel."""
    with _METRICS._lock:
        _METRICS._buf.clear()


def record_samples_for_tests(samples: Iterable[tuple[str, int, float, float]]) -> None:
    """Inject (path, status, duration_ms, ts) tuples for unit tests."""
    for path, status, dur, ts in samples:
        _METRICS.record(path=path, status=status, duration_ms=dur, ts=ts)


# ---------------------------------------------------------------------------
# DETAIL ring buffer — per-request headers + (capped) body for the SPA's
# HTTP inspector. Separate from the aggregate ring buffer because samples
# are larger and capacity is much smaller.
# ---------------------------------------------------------------------------
def _detail_capacity() -> int:
    raw = os.environ.get("REQUEST_DETAIL_CAPACITY", "")
    if raw.isdigit():
        n = int(raw)
        if 16 <= n <= 4096:
            return n
    return DETAIL_CAPACITY_DEFAULT


@dataclass(frozen=True, slots=True)
class _DetailSample:
    ts: float
    request_id: str
    method: str
    path: str  # raw path (not collapsed) — operator wants the actual URL
    status: int
    duration_ms: float
    caller: str | None
    client_ip: str | None
    request_headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    request_body: str | None = None
    request_body_truncated: bool = False
    response_headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    response_body: str | None = None
    response_body_truncated: bool = False
    response_size_bytes: int | None = None

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        # Convert tuple-of-tuples to list-of-objects for JSON friendliness.
        d["request_headers"] = [{"name": k, "value": v} for k, v in self.request_headers]
        d["response_headers"] = [{"name": k, "value": v} for k, v in self.response_headers]
        return d


def redact_headers(headers: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Return headers with sensitive values replaced by a redaction sentinel.

    Header *names* are kept as-is so the operator can still see that an
    Authorization header was present. Only the value is redacted.
    """
    out: list[tuple[str, str]] = []
    for name, value in headers:
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if name.lower() in DETAIL_REDACT_HEADERS:
            out.append((name, DETAIL_REDACT_PLACEHOLDER))
        else:
            out.append((name, value))
    return tuple(out)


def is_capturable_content_type(content_type: str | None) -> bool:
    """True iff a body of this Content-Type may be safely text-captured."""
    if not content_type:
        return False
    ct = content_type.split(";", 1)[0].strip().lower()
    return any(
        ct == prefix.rstrip("/") or ct.startswith(prefix) for prefix in DETAIL_CAPTURABLE_TYPES
    )


def capture_body(
    raw: bytes | None,
    *,
    content_type: str | None,
    cap: int = DETAIL_BODY_CAP_BYTES,
) -> tuple[str | None, bool]:
    """Decode + cap a body for inspector display.

    Returns (text, truncated). Returns (None, False) when there is no body
    or the Content-Type is not safe to render as text.
    """
    if not raw:
        return (None, False)
    if not is_capturable_content_type(content_type):
        return (f"<binary {len(raw)} bytes — not captured>", False)
    truncated = len(raw) > cap
    body = raw[:cap]
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        text = repr(body)
    return (text, truncated)


class _DetailRingBuffer:
    def __init__(self, capacity: int) -> None:
        self._buf: deque[_DetailSample] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._capacity = capacity

    @property
    def capacity(self) -> int:
        return self._capacity

    def record(self, sample: _DetailSample) -> None:
        with self._lock:
            self._buf.append(sample)

    def list_recent(self, *, limit: int = 200) -> list[dict[str, object]]:
        limit = max(1, min(int(limit), self._capacity))
        with self._lock:
            samples = list(self._buf)[-limit:]
        # Return newest-first so the SPA can render a live feed without resort.
        samples.reverse()
        return [s.to_dict() for s in samples]

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


_DETAILS = _DetailRingBuffer(capacity=_detail_capacity())


def details() -> _DetailRingBuffer:
    return _DETAILS


def record_detail(
    *,
    request_id: str,
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    caller: str | None,
    client_ip: str | None,
    request_headers: Iterable[tuple[str, str]],
    request_body: bytes | None,
    request_content_type: str | None,
    response_headers: Iterable[tuple[str, str]],
    response_body: bytes | None,
    response_content_type: str | None,
    response_size_bytes: int | None = None,
    ts: float | None = None,
) -> None:
    """Capture one inspector sample. Always safe to call (never raises on malformed input)."""
    try:
        req_text, req_trunc = capture_body(request_body, content_type=request_content_type)
        res_text, res_trunc = capture_body(response_body, content_type=response_content_type)
        # Cap raw path display length too — we keep the raw path here for
        # the inspector (operator wants the actual URL) but mustn't bloat
        # the buffer with arbitrary fuzz traffic.
        safe_path = path if len(path) <= MAX_PATH_LEN else path[: MAX_PATH_LEN - 1] + "…"
        sample = _DetailSample(
            ts=float(ts if ts is not None else time.time()),
            request_id=str(request_id)[:64] or "-",
            method=str(method).upper()[:8] or "GET",
            path=safe_path,
            status=int(status),
            duration_ms=max(0.0, float(duration_ms)),
            caller=(str(caller)[:128] if caller else None),
            client_ip=(str(client_ip)[:64] if client_ip else None),
            request_headers=redact_headers(request_headers),
            request_body=req_text,
            request_body_truncated=req_trunc,
            response_headers=redact_headers(response_headers),
            response_body=res_text,
            response_body_truncated=res_trunc,
            response_size_bytes=(
                int(response_size_bytes) if response_size_bytes is not None else None
            ),
        )
        _DETAILS.record(sample)
    except Exception:
        return


def reset_details_for_tests() -> None:
    """Clear the detail buffer. ONLY for unit tests."""
    _DETAILS.clear()
