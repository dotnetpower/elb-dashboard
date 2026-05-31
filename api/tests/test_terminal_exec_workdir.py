"""Per-request workdir isolation contract for the terminal exec server.

Stage 2 of issue #23 (BLAST capacity gate) asks whether two parallel
``elastic-blast submit`` invocations can collide on a shared workdir. The
exec server's ``_make_cwd(explicit=None)`` already returns a unique
``tempfile.mkdtemp`` per request, which is strictly stronger than the
per-job isolation the design doc calls for. This test pins that contract
so a future refactor cannot silently fall back to a shared workdir.

Responsibility: Pin the per-request workdir isolation guarantee that the
BLAST capacity gate Stage 2 verification relies on.
Edit boundaries: Imports the exec server module by file path so we don't
need to add the terminal sidecar to PYTHONPATH at suite level. Keep this
test pure — no Redis, no Celery, no Azure SDK.
Key entry points: ``test_make_cwd_per_request_isolation``,
``test_make_cwd_explicit_path_is_unowned``,
``test_make_cwd_uses_request_prefix``.
Risky contracts: ``_make_cwd`` is the load-bearing isolation primitive for
every ``/exec/run`` and ``/exec/stream`` call. If two parallel calls ever
share a cwd the BLAST submit path would race on ``elastic-blast.ini``.
Validation: ``uv run pytest -q api/tests/test_terminal_exec_workdir.py``.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXEC_SERVER_PATH = _REPO_ROOT / "terminal" / "exec_server.py"


def _load_exec_server_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "_elb_exec_server_for_test", _EXEC_SERVER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def exec_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> object:
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.setenv("EXEC_TOKEN", "test-token")
    module = _load_exec_server_module()
    monkeypatch.setattr(module, "EXEC_TMP_ROOT", str(tmp_path), raising=False)
    return module


def test_make_cwd_per_request_isolation(exec_server: object) -> None:
    cwd1, owned1 = exec_server._make_cwd(None)
    cwd2, owned2 = exec_server._make_cwd(None)
    try:
        assert owned1 is True
        assert owned2 is True
        assert cwd1 != cwd2
        assert os.path.isdir(cwd1)
        assert os.path.isdir(cwd2)
    finally:
        shutil.rmtree(cwd1, ignore_errors=True)
        shutil.rmtree(cwd2, ignore_errors=True)


def test_make_cwd_uses_request_prefix(exec_server: object) -> None:
    cwd, owned = exec_server._make_cwd(None)
    try:
        assert owned is True
        assert os.path.basename(cwd).startswith("req-")
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


def test_make_cwd_explicit_path_is_unowned(exec_server: object, tmp_path: Path) -> None:
    explicit = str(tmp_path / "caller-owned")
    cwd, owned = exec_server._make_cwd(explicit)
    assert cwd == explicit
    assert owned is False
