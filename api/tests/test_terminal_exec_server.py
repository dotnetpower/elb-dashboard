"""Behavioural + static guards for the terminal sidecar exec server.

Responsibility: Lock in the exec server's spawn-failure diagnostic contract and
the deploy-time pinning that keeps the ``elastic-blast`` launcher present in the
terminal toolchain image.
Edit boundaries: Pure in-process tests — the spawn-failure case monkeypatches
``exec_server._spawn`` so no real subprocess is started (keeps the test in the
default dev loop / CI, unlike the subprocess-marked toolchain tests). The static
guards only read repository files.
Key entry points: ``test_stream_reports_missing_binary_as_diagnostic``,
``test_run_buffered_reports_missing_binary``,
``test_terminal_base_pins_ref_that_ships_elastic_blast_launcher``,
``test_terminal_dockerfiles_guard_elastic_blast_executable``.
Risky contracts: The exec server's streaming response has already sent HTTP 200
before spawning the child, so a missing binary MUST be surfaced as a stderr line
plus a non-zero (127) summary — letting the OSError escape yields an empty body
that the api caller reports as the opaque "no output captured".
Validation: ``uv run pytest -q api/tests/test_terminal_exec_server.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXEC_SERVER_PATH = REPO_ROOT / "terminal" / "exec_server.py"
TERMINAL_BASE_IMAGE_SH = REPO_ROOT / "scripts" / "dev" / "terminal-base-image.sh"
DOCKERFILE_BASE = REPO_ROOT / "terminal" / "Dockerfile.base"
DOCKERFILE = REPO_ROOT / "terminal" / "Dockerfile"

# The sibling ref the terminal toolchain must build from. It is the last commit
# that still ships the ``bin/`` launcher scripts (``setup.cfg`` installs the
# ``elastic-blast`` CLI via ``scripts = bin/*``); upstream master removed
# ``bin/`` in commit 72a69822, which silently drops the executable.
PINNED_ELASTIC_BLAST_REF = "f4b8b734a82285a18a2ca9aadcbe02759d13f903"


def _load_exec_server() -> ModuleType:
    """Import terminal/exec_server.py under a throwaway module name.

    The module's loopback-bind guard only fires when ``CONTAINER_APP_NAME`` is
    set, so a plain import in the test environment is safe.
    """
    sys.modules.pop("exec_server", None)
    spec = importlib.util.spec_from_file_location("exec_server", EXEC_SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def exec_server() -> Iterator[ModuleType]:
    module = _load_exec_server()
    try:
        yield module
    finally:
        sys.modules.pop("exec_server", None)


def test_stream_reports_missing_binary_as_diagnostic(
    exec_server: ModuleType, tmp_path: Path
) -> None:
    # The streaming handler has already flushed HTTP 200 + headers before it
    # calls ``_stream``; a missing binary must therefore be reported in-band as
    # a stderr line + a non-zero (127) summary, not by letting the OSError
    # escape (which yields an empty body → "no output captured").
    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory", "elastic-blast")

    exec_server._spawn = _raise  # type: ignore[attr-defined]

    lines: list[dict[str, Any]] = []
    req = {
        "argv": ["elastic-blast", "submit", "--cfg", "elastic-blast.ini"],
        "stdin": None,
        "stdin_file": None,
        "cwd": str(tmp_path),  # explicit cwd → _stream does not own/clean it
        "timeout": 5,
    }

    summary = exec_server._stream(req, lines.append)

    assert summary["exit_code"] == 127
    assert summary["timed_out"] is False
    assert "cannot start 'elastic-blast'" in summary["error"]
    # The diagnostic is also streamed as a real stderr line so the api caller
    # captures it as command output.
    stderr_lines = [item for item in lines if item.get("stream") == "stderr"]
    assert stderr_lines, "expected a stderr diagnostic line"
    assert "cannot start 'elastic-blast'" in stderr_lines[-1]["line"]


def test_run_buffered_reports_missing_binary(
    exec_server: ModuleType, tmp_path: Path
) -> None:
    # The buffered path can let the OSError escape (its handler has not sent a
    # response yet, so do_POST turns it into a 500 with the detail). Assert the
    # error is not swallowed into a clean exit-0 result.
    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory", "elastic-blast")

    exec_server._spawn = _raise  # type: ignore[attr-defined]

    req = {
        "argv": ["elastic-blast", "submit"],
        "stdin": None,
        "stdin_file": None,
        "cwd": str(tmp_path),
        "timeout": 5,
    }

    with pytest.raises(FileNotFoundError):
        exec_server._run_buffered(req)


def test_terminal_base_pins_ref_that_ships_elastic_blast_launcher() -> None:
    # Root-cause guard: the deploy helper must NOT default the build ref to
    # ``master`` (which removed bin/* and thus the elastic-blast executable).
    # It must pin the known-good ref and resolve the hash + build through the
    # same default so the content tag can never drift from the built ref.
    body = TERMINAL_BASE_IMAGE_SH.read_text()
    assert PINNED_ELASTIC_BLAST_REF in body
    assert "_ELASTIC_BLAST_REF_DEFAULT" in body
    # No remaining ``:-master`` default on the build arg / hash.
    assert "ELASTIC_BLAST_REF:-master" not in body


def test_terminal_dockerfiles_guard_elastic_blast_executable() -> None:
    # Defense in depth: both terminal Dockerfiles pin the good ref and fail the
    # build loudly if the install did not produce the elastic-blast launcher.
    for dockerfile in (DOCKERFILE_BASE, DOCKERFILE):
        body = dockerfile.read_text()
        assert f"ARG ELASTIC_BLAST_REF={PINNED_ELASTIC_BLAST_REF}" in body
        assert "command -v elastic-blast" in body
        assert "ELASTIC_BLAST_REF=master" not in body
