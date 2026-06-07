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


def test_writer_flushes_on_time_interval_below_size_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small (sub-64-KiB) build must flush incrementally on the time trigger
    so the live viewer is not empty until the build finishes."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(build_logs.time, "monotonic", lambda: clock["t"])
    writer = build_logs.open_writer("jobTime1", "api")

    # First small line within the interval: buffered, not yet flushed.
    writer.write_line("step 1/5")
    assert build_logs.read_blob("jobTime1", "api") == b""

    # Advance past the flush interval; the next line triggers a time-based flush
    # that drains the whole buffer even though it is far below 64 KiB.
    clock["t"] += build_logs._FLUSH_INTERVAL_SECONDS + 0.1
    writer.write_line("step 2/5")
    assert build_logs.read_blob("jobTime1", "api") == b"step 1/5\nstep 2/5\n"



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


def test_secret_scanner_masks_common_token_shapes() -> None:
    """Defence in depth: the persisted build log must never carry
    bearer/AWS/GitHub/password-shaped values verbatim, even if a
    future Dockerfile echoes one during build.
    """
    from api.services.upgrade.build_logs import _mask_secrets

    cases = [
        "Authorization: Bearer abc123ABCDEF.xyzqwertyuiopasdfghjkl",
        'aws_access_key: "AKIAIOSFODNN7EXAMPLE"',
        "export GITHUB_TOKEN=ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "db_password=hunter2isagreatpassword",
        "cloning https://user:supersecret@github.com/foo/bar.git",
    ]
    for raw in cases:
        masked = _mask_secrets(raw)
        assert "***REDACTED***" in masked, f"failed to mask: {raw!r} -> {masked!r}"
    combined = " || ".join(_mask_secrets(r) for r in cases)
    assert "hunter2isagreatpassword" not in combined
    assert "supersecret" not in combined
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in combined
    assert "AKIAIOSFODNN7EXAMPLE" not in combined


def test_writer_masks_secrets_at_write_time() -> None:
    """`BuildLogWriter.write_line` must mask before persistence so even
    a leak in the in-memory buffer is already redacted.
    """
    writer = build_logs.open_writer("jobMask1", "api")
    writer.write_line("step 1: ok")
    writer.write_line("Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig")
    writer.write_line("done")
    writer.flush()
    payload = build_logs.read_blob("jobMask1", "api").decode()
    assert "step 1: ok" in payload
    assert "done" in payload
    assert "eyJhbGc" not in payload
    assert "***REDACTED***" in payload


def test_writer_buffer_caps_when_backend_persistently_fails() -> None:
    """With a backend that never accepts a flush, the retain-on-failure
    path must drop the OLDEST bytes once the per-writer ceiling is hit
    so a sustained Storage outage does not OOM the worker. The tail —
    which is what an operator reads first — is preserved, and a marker
    line tells the reader the gap exists.
    """

    class _AlwaysFail:
        def create(self, name: str) -> None:
            pass

        def append(self, name: str, payload: bytes) -> None:
            raise RuntimeError("backend offline")

        def read(self, name: str) -> bytes:
            raise KeyError(name)

    build_logs.set_backend(_AlwaysFail())
    # Force the cap low so the test does not allocate 16 MiB.
    import api.services.upgrade.build_logs as bl

    original_cap = bl._MAX_BUFFER_BYTES
    bl._MAX_BUFFER_BYTES = 1024  # 1 KiB ceiling for the test
    try:
        writer = build_logs.open_writer("jobBigBuf", "api")
        # Each line ~= 64 B; push enough to exceed the ceiling several times.
        for i in range(200):
            writer.write_line(f"L{i:04d} " + "x" * 50)
        # Drain whatever is buffered (will keep failing).
        for _ in range(5):
            try:
                writer.flush()
            except RuntimeError:
                pass
        buf = bytes(writer._buffer)
        assert len(buf) <= bl._MAX_BUFFER_BYTES
        # Truncation marker tells the reader some lines were dropped.
        assert b"build log truncated" in buf
        # Newest line (L0199) survives.
        assert b"L0199" in buf
        # Oldest line (L0000) was dropped to make room.
        assert b"L0000" not in buf
    finally:
        bl._MAX_BUFFER_BYTES = original_cap
