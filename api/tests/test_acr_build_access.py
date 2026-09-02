"""Tests for temporary ACR build-access network policy restoration.

Responsibility: Verify deploy builds open ACR only temporarily and safely heal a
stale public-Allow posture without interrupting another active ACR Task.
Edit boundaries: Test-only Bash harness with a fake ``az`` function; never call
Azure or mutate a real registry.
Key entry points: pytest test functions.
Risky contracts: Unknown or non-zero active-build state must fail safe by leaving
access open; an idle stale-open registry must return to Disabled/Deny.
Validation: ``uv run pytest -q api/tests/test_acr_build_access.py -m subprocess``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "dev" / "acr-build-access.sh"


def _run_harness(
    tmp_path: Path,
    *,
    initial_state: str,
    running_builds: str = "0",
    running_query_fails: bool = False,
    preserve_open: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    calls = tmp_path / "az-calls.log"
    logs = tmp_path / "messages.log"
    env = {
        **os.environ,
        "CALLS": str(calls),
        "LOGS": str(logs),
        "INITIAL_STATE": initial_state,
        "RUNNING_BUILDS": running_builds,
        "RUNNING_QUERY_FAILS": "1" if running_query_fails else "0",
        "ACR_BUILD_ACCESS_PRESERVE_OPEN": "1" if preserve_open else "0",
        "ACR_BUILD_ACCESS_SETTLE_SECONDS": "0",
    }
    command = f"""
set -euo pipefail
source '{_SCRIPT}'
acr_build_access_log() {{ printf '%s\n' "$*" >> "$LOGS"; }}
acr_show_network_state() {{ printf '%s\n' "$INITIAL_STATE"; }}
acr_wait_for_build_access_state() {{ return 0; }}
acr_active_build_count() {{
  if [[ "$RUNNING_QUERY_FAILS" == "1" ]]; then return 1; fi
  printf '%s\n' "$RUNNING_BUILDS"
}}
az() {{ printf '%s\n' "$*" >> "$CALLS"; }}
acr_ensure_build_access testregistry
acr_restore_build_access testregistry
printf 'restore_needed=%s steady=%s\n' \
  "${{ACR_BUILD_ACCESS_RESTORE_NEEDED:-unset}}" \
  "${{ACR_BUILD_ACCESS_RESTORE_TO_STEADY_STATE:-unset}}"
"""
    result = subprocess.run(  # noqa: S603 - executes a checked-in shell helper with fakes.
        ["/bin/bash", "-c", command],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return (
        result,
        calls.read_text() if calls.exists() else "",
        logs.read_text() if logs.exists() else "",
    )


def test_idle_stale_open_registry_is_restored_private(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="Enabled Allow AzureServices",
    )

    assert result.returncode == 0, result.stderr
    assert "--public-network-enabled false" in calls
    assert "--default-action Deny" in calls
    assert "will restore private steady state" in logs
    assert result.stdout == "restore_needed=0 steady=0\n"


def test_active_other_build_leaves_stale_open_registry_unchanged(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="Enabled Allow AzureServices",
        running_builds="1",
    )

    assert result.returncode == 0, result.stderr
    assert calls == ""
    assert "other ACR build(s) still active" in logs
    assert result.stdout == "restore_needed=1 steady=1\n"


def test_unknown_active_build_state_fails_safe_open(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="Enabled Allow AzureServices",
        running_query_fails=True,
    )

    assert result.returncode == 0, result.stderr
    assert calls == ""
    assert "could not verify active ACR builds" in logs


def test_explicit_preserve_keeps_open_registry(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="Enabled Allow AzureServices",
        preserve_open=True,
    )

    assert result.returncode == 0, result.stderr
    assert calls == ""
    assert "explicit preserve requested" in logs
    assert result.stdout == "restore_needed=0 steady=0\n"


def test_private_registry_is_opened_then_restored(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="Disabled Deny AzureServices",
    )

    assert result.returncode == 0, result.stderr
    call_lines = calls.splitlines()
    assert len(call_lines) == 2
    assert "--public-network-enabled true" in call_lines[0]
    assert "--public-network-enabled false" in call_lines[1]
    assert "Opening ACR build access temporarily" in logs
    assert "Restoring ACR network policy" in logs


def test_private_origin_defers_restore_while_other_build_is_active(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="Disabled Deny AzureServices",
        running_builds="1",
    )

    assert result.returncode == 0, result.stderr
    assert len(calls.splitlines()) == 1
    assert "--public-network-enabled true" in calls
    assert "other ACR build(s) still active" in logs
    assert result.stdout == "restore_needed=1 steady=0\n"


def test_active_build_query_covers_queue_start_and_run_states() -> None:
    source = _SCRIPT.read_text()

    assert "status=='Queued'" in source
    assert "status=='Started'" in source
    assert "status=='Running'" in source
