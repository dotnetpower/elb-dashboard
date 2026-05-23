"""Append-blob log writers for the upgrade build pipeline.

Module summary: Captures `az acr build` stdout/stderr lines into a
per-component Append Blob (`upgrade-logs/<job_id>/build-<component>.log`)
so an operator can review the build evidence even after the producing
revision has been rolled. The backend is swappable so unit tests run
entirely in-memory. Lines are scanned for token-like patterns and the
matched value is masked before persistence — defence in depth against
a future Docker build context accidentally echoing a secret.

Responsibility: Persistent per-component build logs for the upgrade flow.
Edit boundaries: Blob naming and creation live here; routes/tasks just
  call `open_writer()` / `read_blob()`.
Key entry points: `BuildLogWriter`, `open_writer`, `read_blob`,
  `blob_name`, `set_backend`, `InMemoryBuildLogBackend`, `_mask_secrets`.
Risky contracts: Append-blob block size is bounded; writers chunk lines
  so a runaway producer can't blow past Azure's 4 MiB block ceiling.
  Tests must use `InMemoryBuildLogBackend` (guarded by
  `PYTEST_CURRENT_TEST`).
Validation: `uv run pytest -q api/tests/test_upgrade_build_logs.py`.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Iterable
from typing import Protocol

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from api.services import get_credential

LOGGER = logging.getLogger(__name__)

BUILD_LOG_CONTAINER = "upgrade-logs"
_BLOB_ENDPOINT_ENV = "AZURE_BLOB_ENDPOINT"
_MAX_BLOCK_BYTES = 3 * 1024 * 1024  # 3 MiB; Azure caps at 4 MiB per append.
# Hard ceiling on the per-writer in-process buffer. When the backend
# repeatedly fails to flush (Storage 503, throttling, network blip) the
# `_flush_locked` retry path puts the payload back so a transient outage
# does not lose lines. Without a ceiling, a sustained outage causes the
# api / worker process RSS to grow until OOM-kill. 16 MiB is generous
# (~150K log lines) vs the typical `az acr build` output and is dropped
# oldest-first with a marker so the tail — which is what an operator
# actually reads — is always preserved.
_MAX_BUFFER_BYTES = 16 * 1024 * 1024
_TRUNCATION_MARKER = b"\n... [build log truncated: backend backpressure dropped older lines] ...\n"

# Token-like patterns that should never appear verbatim in a persisted
# build log. Defence in depth: the upgrade pipeline already scrubs the
# remote URL's `userinfo` and never passes credentials as `--build-arg`,
# but a future Dockerfile that echoes an env at build time could land
# a real secret here. We mask on write so the persisted blob is safe
# to share with a wider operator audience.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Bearer / Authorization tokens
    ("BEARER", re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]{16,})")),
    ("AUTH_BASIC", re.compile(r"(?i)\b(basic\s+)([A-Za-z0-9+/=]{16,})")),
    # AWS access key id / secret
    ("AWS_ACCESS_KEY_ID", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    (
        "AWS_SECRET",
        re.compile(
            r"(?i)(aws[_-]?secret[_-]?access[_-]?key\s*[:=]\s*)([A-Za-z0-9/+=]{30,})"
        ),
    ),
    # GitHub / GitLab classic + fine-grained tokens
    ("GITHUB_TOKEN", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,})\b")),
    ("GITHUB_FG", re.compile(r"\b(github_pat_[A-Za-z0-9_]{30,})\b")),
    # generic password-looking key/value (foo_pass=..., FOO_PASSWORD=...)
    (
        "PASSWORD_KV",
        re.compile(
            r"(?i)((?:password|passwd|pwd|secret|token)\s*[:=]\s*)"
            r"['\"]?([^\s'\"&]{8,})"
        ),
    ),
    # URL credentials (https://user:pw@host)
    (
        "URL_CRED",
        re.compile(r"(?i)\b(https?://[^\s/:@]+):([^\s/@]+)@"),
    ),
)


def _mask_secrets(line: str) -> str:
    """Return ``line`` with any matched secret-shaped substring redacted.

    The mask preserves the surrounding context so a build-log reader
    still sees ``Bearer ***REDACTED***`` (not just whitespace) and can
    confirm a token was emitted at that point. Multiple patterns may
    match the same line; each is applied independently.
    """
    if not line:
        return line
    out = line
    for _label, pattern in _SECRET_PATTERNS:
        out = pattern.sub(_replace_secret, out)
    return out


def _replace_secret(match: re.Match[str]) -> str:
    """Helper: keep group(1) (the label/prefix), redact group(2)/whole."""
    if match.lastindex and match.lastindex >= 2:
        prefix = match.group(1)
        return f"{prefix}***REDACTED***"
    return "***REDACTED***"


def blob_name(job_id: str, component: str) -> str:
    if not job_id or not component:
        raise ValueError("job_id and component must be non-empty")
    if any(c in job_id for c in "/\\.") or any(c in component for c in "/\\."):
        raise ValueError(f"unsafe job_id/component: {job_id!r}/{component!r}")
    return f"{job_id}/build-{component}.log"


# ---------------------------------------------------------------------------
# Backend abstraction so tests run without an Azure Blob endpoint.
# ---------------------------------------------------------------------------


class _Backend(Protocol):
    def create(self, name: str) -> None: ...

    def append(self, name: str, payload: bytes) -> None: ...

    def read(self, name: str) -> bytes: ...


class InMemoryBuildLogBackend:
    """Test-only backend storing blobs in a dict."""

    def __init__(self) -> None:
        if not os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get(
            "ELB_ALLOW_INMEMORY_BUILD_LOGS", ""
        ).lower() != "true":
            raise RuntimeError(
                "InMemoryBuildLogBackend is for tests only; set "
                "ELB_ALLOW_INMEMORY_BUILD_LOGS=true to opt in."
            )
        self._blobs: dict[str, bytearray] = {}
        self._lock = threading.Lock()

    def create(self, name: str) -> None:
        with self._lock:
            self._blobs.setdefault(name, bytearray())

    def append(self, name: str, payload: bytes) -> None:
        with self._lock:
            if name not in self._blobs:
                self._blobs[name] = bytearray()
            self._blobs[name].extend(payload)

    def read(self, name: str) -> bytes:
        with self._lock:
            if name not in self._blobs:
                raise KeyError(name)
            return bytes(self._blobs[name])


class _AzureAppendBlobBackend:
    """Production backend using Azure Append Blobs."""

    def __init__(self) -> None:
        endpoint = os.environ.get(_BLOB_ENDPOINT_ENV, "").strip()
        if not endpoint:
            raise RuntimeError(
                f"{_BLOB_ENDPOINT_ENV} is not set; upgrade build logs require Azure Blob."
            )
        # Lazy-import the Azure SDK so test environments without
        # azure-storage-blob still load the module fine.
        from azure.storage.blob import BlobServiceClient

        self._svc = BlobServiceClient(account_url=endpoint, credential=get_credential())
        self._ensured = False
        self._ensure_lock = threading.Lock()

    def _container(self):  # type: ignore[no-untyped-def]
        if not self._ensured:
            with self._ensure_lock:
                if not self._ensured:
                    try:
                        self._svc.create_container(BUILD_LOG_CONTAINER)
                    except ResourceExistsError:
                        pass
                    self._ensured = True
        return self._svc.get_container_client(BUILD_LOG_CONTAINER)

    def create(self, name: str) -> None:
        blob = self._container().get_blob_client(name)
        try:
            blob.create_append_blob()
        except ResourceExistsError:
            pass

    def append(self, name: str, payload: bytes) -> None:
        blob = self._container().get_blob_client(name)
        # Chunk so we never exceed Azure's per-append 4 MiB ceiling.
        for offset in range(0, len(payload), _MAX_BLOCK_BYTES):
            blob.append_block(payload[offset : offset + _MAX_BLOCK_BYTES])

    def read(self, name: str) -> bytes:
        blob = self._container().get_blob_client(name)
        try:
            from api.services.storage.data import (
                METADATA_BLOB_MAX_BYTES,
                read_metadata_blob_bytes,
            )

            return read_metadata_blob_bytes(
                blob, max_bytes=METADATA_BLOB_MAX_BYTES, label=f"upgrade-build-log:{name}"
            )
        except ResourceNotFoundError as exc:
            raise KeyError(name) from exc


_BACKEND_LOCK = threading.Lock()
_BACKEND: _Backend | None = None


def set_backend(backend: _Backend | None) -> None:
    global _BACKEND
    with _BACKEND_LOCK:
        _BACKEND = backend


def _backend() -> _Backend:
    if _BACKEND is not None:
        return _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is not None:
            return _BACKEND
        return _AzureAppendBlobBackend()


class BuildLogWriter:
    """Per-component log writer; instantiate via `open_writer()`."""

    def __init__(self, name: str, backend: _Backend) -> None:
        self._name = name
        self._backend = backend
        self._buffer = bytearray()
        self._lock = threading.Lock()
        backend.create(name)

    @property
    def name(self) -> str:
        return self._name

    def write_line(self, line: str) -> None:
        masked = _mask_secrets(line)
        encoded = (masked.rstrip("\n") + "\n").encode("utf-8", errors="replace")
        with self._lock:
            self._buffer.extend(encoded)
            if len(self._buffer) >= 64 * 1024:  # flush every ~64 KiB
                self._flush_locked()

    def write_lines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write_line(line)

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        payload = bytes(self._buffer)
        self._buffer.clear()
        try:
            self._backend.append(self._name, payload)
        except Exception:
            LOGGER.exception(
                "upgrade.build_logs: failed to append %d bytes to %s",
                len(payload),
                self._name,
            )
            # Put the bytes back so a future flush can retry, but enforce
            # the buffer ceiling so a backend in sustained failure mode
            # never grows our RSS without bound. Drop oldest — the tail
            # is what the operator reads.
            combined = bytearray(payload) + self._buffer
            if len(combined) > _MAX_BUFFER_BYTES:
                overflow = len(combined) - _MAX_BUFFER_BYTES + len(_TRUNCATION_MARKER)
                if overflow < len(combined):
                    combined = bytearray(_TRUNCATION_MARKER) + combined[overflow:]
                else:
                    # The payload alone exceeds the ceiling; keep the tail.
                    tail_budget = _MAX_BUFFER_BYTES - len(_TRUNCATION_MARKER)
                    combined = bytearray(_TRUNCATION_MARKER) + combined[-tail_budget:]
            self._buffer = combined
            raise


def open_writer(job_id: str, component: str) -> BuildLogWriter:
    """Create (or open) an append-blob writer for the given job/component."""
    name = blob_name(job_id, component)
    return BuildLogWriter(name, _backend())


def read_blob(job_id: str, component: str) -> bytes:
    """Read the full append blob (or raise KeyError when missing)."""
    return _backend().read(blob_name(job_id, component))
