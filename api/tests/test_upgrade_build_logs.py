"""Tests for the upgrade build-log Append Blob writer (in-memory backend).

Module summary: Drives `BuildLogWriter` against `InMemoryBuildLogBackend`
so no Azure Blob endpoint is required.

Responsibility: Verify blob naming guards, append semantics, and flush
  behaviour.
Edit boundaries: Update when blob-naming or chunking contracts change.
Key entry points: Tests for happy-path write/read, name guards, flush
  retention on backend failure.
Risky contracts: Asserts that the buffered bytes are restored to the
  internal buffer if a backend append fails, so a retry preserves the
  payload.
Validation: `uv run pytest -q api/tests/test_upgrade_build_logs.py`.
"""

from __future__ import annotations

import pytest
from api.services.upgrade import build_logs


@pytest.fixture(autouse=True)
def _in_memory_backend() -> None:
    build_logs.set_backend(build_logs.InMemoryBuildLogBackend())
    yield
    build_logs.set_backend(None)


def test_blob_name_rejects_unsafe_inputs() -> None:
    with pytest.raises(ValueError):
        build_logs.blob_name("", "api")
    with pytest.raises(ValueError):
        build_logs.blob_name("job/1", "api")
    with pytest.raises(ValueError):
        build_logs.blob_name("job1", "../api")


def test_writer_flushes_buffered_lines() -> None:
    writer = build_logs.open_writer("jobABCD", "api")
    writer.write_line("hello")
    writer.write_line("world\n")
    writer.flush()
    payload = build_logs.read_blob("jobABCD", "api")
    assert payload == b"hello\nworld\n"


def test_write_lines_iterable() -> None:
    writer = build_logs.open_writer("jobXYZW", "frontend")
    writer.write_lines(["a", "b", "c"])
    writer.flush()
    payload = build_logs.read_blob("jobXYZW", "frontend")
    assert payload == b"a\nb\nc\n"


def test_writer_recovers_buffer_on_backend_failure() -> None:
    class _BoomBackend:
        def __init__(self) -> None:
            self.append_calls = 0

        def create(self, name: str) -> None:
            pass

        def append(self, name: str, payload: bytes) -> None:
            self.append_calls += 1
            raise RuntimeError("simulated backend failure")

        def read(self, name: str) -> bytes:
            raise KeyError(name)

    backend = _BoomBackend()
    build_logs.set_backend(backend)
    writer = build_logs.open_writer("jobErr1", "terminal")
    writer.write_line("first line that will fail to flush")
    with pytest.raises(RuntimeError):
        writer.flush()
    # Buffer was restored — internal byte-array still has the unsynced payload.
    assert b"first line" in bytes(writer._buffer)
