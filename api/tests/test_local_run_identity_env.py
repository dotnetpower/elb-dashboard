"""Local launcher identity-metadata environment contract tests.

Responsibility: Verify local-run loads the deployed dashboard principal ID for
    API, worker, and VS Code debug sessions without changing credential choice.
Edit boundaries: Shell environment loading in scripts/dev/local-run.sh only.
Key entry points: `load_local_shared_identity_env`, the `debug-env` command.
Risky contracts: Explicit shell overrides must win over azd values, and only the
    principal ID may be imported so local Azure CLI authentication is preserved.
Validation: `uv run pytest -q api/tests/test_local_run_identity_env.py -m ""`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_RUN = _REPO_ROOT / "scripts" / "dev" / "local-run.sh"


def _identity_loader_functions() -> str:
    script = _LOCAL_RUN.read_text(encoding="utf-8")
    start = script.index("azd_env_value() {")
    end = script.index("\nvalidate_azure_cli_context() {", start)
    return script[start:end]


def test_azd_env_lookup_is_bounded_and_noninteractive() -> None:
    functions = _identity_loader_functions()

    assert "timeout 8s azd env get-values </dev/null" in functions
    assert "azd env get-values </dev/null" in functions


def _run_identity_loader(
    tmp_path: Path,
    *,
    explicit_principal_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_azd = tmp_path / "azd"
    fake_azd.write_text(
        """#!/usr/bin/env bash
if [[ "$*" == "env get-values" ]]; then
  printf '%s\\n' 'SHARED_IDENTITY_PRINCIPAL_ID="principal-from-azd"'
  exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    fake_azd.chmod(0o755)

    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    env.pop("SHARED_IDENTITY_PRINCIPAL_ID", None)
    if explicit_principal_id is not None:
        env["SHARED_IDENTITY_PRINCIPAL_ID"] = explicit_principal_id

    return subprocess.run(  # noqa: S603 - executes extracted reviewed repo functions.
        [
            "/bin/bash",
            "-c",
            (
                "set -euo pipefail\n"
                f"{_identity_loader_functions()}\n"
                "load_local_shared_identity_env\n"
                "printf '%s' \"$SHARED_IDENTITY_PRINCIPAL_ID\""
            ),
        ],
        cwd=_REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.subprocess
def test_local_run_loads_shared_identity_principal_from_azd(tmp_path: Path) -> None:
    result = _run_identity_loader(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "principal-from-azd"


@pytest.mark.subprocess
def test_local_run_preserves_explicit_shared_identity_principal(tmp_path: Path) -> None:
    result = _run_identity_loader(tmp_path, explicit_principal_id="explicit-principal")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "explicit-principal"


def test_debug_env_includes_shared_identity_principal() -> None:
    script = _LOCAL_RUN.read_text(encoding="utf-8")
    debug_env = script[script.index("  debug-env)") : script.index("  smoke)")]

    assert "load_local_shared_identity_env" in debug_env
    assert 'echo "SHARED_IDENTITY_PRINCIPAL_ID=$SHARED_IDENTITY_PRINCIPAL_ID"' in debug_env
