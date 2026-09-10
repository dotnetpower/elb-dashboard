"""Tests for temporary ACR build-access network policy restoration.

Responsibility: Verify deploy builds open ACR only temporarily and safely heal a
stale public-Allow posture after cancellation without interrupting another active ACR Task.
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
    state_query_fails: bool = False,
    open_update_fails: bool = False,
    restore_update_fails: bool = False,
    open_wait_fails: bool = False,
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
        "STATE_QUERY_FAILS": "1" if state_query_fails else "0",
        "OPEN_UPDATE_FAILS": "1" if open_update_fails else "0",
        "RESTORE_UPDATE_FAILS": "1" if restore_update_fails else "0",
        "OPEN_WAIT_FAILS": "1" if open_wait_fails else "0",
        "ACR_BUILD_ACCESS_PRESERVE_OPEN": "1" if preserve_open else "0",
        "ACR_BUILD_ACCESS_SETTLE_SECONDS": "0",
    }
    command = f"""
set -euo pipefail
source '{_SCRIPT}'
acr_build_access_log() {{ printf '%s\n' "$*" >> "$LOGS"; }}
acr_show_network_state() {{
    if [[ "$STATE_QUERY_FAILS" == "1" ]]; then return 1; fi
    printf '%s\n' "$INITIAL_STATE"
}}
acr_wait_for_build_access_state() {{
    if [[ "$OPEN_WAIT_FAILS" == "1" ]]; then
        acr_build_access_log "simulated open-policy convergence timeout"
        return 1
    fi
    return 0
}}
acr_active_build_count() {{
  if [[ "$RUNNING_QUERY_FAILS" == "1" ]]; then return 1; fi
  printf '%s\n' "$RUNNING_BUILDS"
}}
az() {{
    printf '%s\n' "$*" >> "$CALLS"
    if [[ "$OPEN_UPDATE_FAILS" == "1" && "$*" == *"--public-network-enabled true"* ]]; then
        return 1
    fi
    if [[ "$RESTORE_UPDATE_FAILS" == "1" && "$*" == *"--public-network-enabled false"* ]]; then
        return 1
    fi
}}
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
    assert "--allow-trusted-services true" in calls
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


def test_non_numeric_active_build_state_fails_safe_open(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="Enabled Allow AzureServices",
        running_builds="unexpected-output",
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


def test_private_registry_without_bypass_restores_original_bypass(tmp_path: Path) -> None:
    result, calls, _logs = _run_harness(
        tmp_path,
        initial_state="Disabled Deny None",
    )

    assert result.returncode == 0, result.stderr
    call_lines = calls.splitlines()
    assert "--allow-trusted-services true" in call_lines[0]
    assert "--allow-trusted-services false" in call_lines[1]


def test_unknown_bypass_fails_before_opening(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="Disabled Deny Unexpected",
    )

    assert result.returncode != 0
    assert calls == ""
    assert "network policy response was incomplete" in logs


def test_unknown_initial_network_state_fails_before_opening(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="",
        state_query_fails=True,
    )

    assert result.returncode != 0
    assert calls == ""
    assert "could not read ACR network policy" in logs


def test_restore_update_failure_is_fatal_and_keeps_lease_armed(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="Disabled Deny AzureServices",
        restore_update_fails=True,
    )

    assert result.returncode != 0
    assert "--public-network-enabled true" in calls
    assert "--public-network-enabled false" in calls
    assert "failed to restore ACR network policy" in logs


def test_open_update_failure_is_fatal(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="Disabled Deny AzureServices",
        open_update_fails=True,
    )

    assert result.returncode != 0
    assert "--public-network-enabled true" in calls
    assert "--public-network-enabled false" not in calls
    assert "failed to open ACR build access" in logs


def test_open_policy_convergence_timeout_is_fatal(tmp_path: Path) -> None:
    result, calls, logs = _run_harness(
        tmp_path,
        initial_state="Disabled Deny AzureServices",
        open_wait_fails=True,
    )

    assert result.returncode != 0
    assert "--public-network-enabled true" in calls
    assert "--public-network-enabled false" not in calls
    assert "simulated open-policy convergence timeout" in logs


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
    assert "ACR_BUILD_ACCESS_READY_ATTEMPTS must be between 1 and 120" in source
    assert "ACR_BUILD_ACCESS_READY_INTERVAL_SECONDS must be between 0 and 300" in source


def _run_reconcile_harness(
    tmp_path: Path,
    *,
    initial_state: str,
    active_builds: list[str],
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    calls = tmp_path / "az-calls.log"
    logs = tmp_path / "messages.log"
    state = tmp_path / "state.txt"
    builds = tmp_path / "builds.txt"
    state.write_text(initial_state)
    builds.write_text("\n".join(active_builds) + "\n")
    command = f"""
set -euo pipefail
source '{_SCRIPT}'
acr_build_access_log() {{ printf '%s\n' "$*" >> "$LOGS"; }}
acr_show_network_state() {{ cat "$STATE"; }}
acr_active_build_count() {{
  local value
  value=$(head -n 1 "$BUILDS")
  tail -n +2 "$BUILDS" > "$BUILDS.next"
  mv "$BUILDS.next" "$BUILDS"
  printf '%s\n' "$value"
}}
sleep() {{ :; }}
az() {{
  printf '%s\n' "$*" >> "$CALLS"
  if [[ "$*" == *"--public-network-enabled false"* ]]; then
    printf 'Disabled Deny AzureServices\n' > "$STATE"
  fi
}}
acr_reconcile_private_steady_state testregistry
"""
    result = subprocess.run(  # noqa: S603 - executes a checked-in shell helper with fakes.
        ["/bin/bash", "-c", command],
        env={
            **os.environ,
            "CALLS": str(calls),
            "LOGS": str(logs),
            "STATE": str(state),
            "BUILDS": str(builds),
            "ACR_BUILD_ACCESS_IDLE_ATTEMPTS": str(len(active_builds)),
            "ACR_BUILD_ACCESS_IDLE_INTERVAL_SECONDS": "0",
            "ACR_BUILD_ACCESS_PRIVATE_VERIFY_ATTEMPTS": "2",
            "ACR_BUILD_ACCESS_PRIVATE_VERIFY_INTERVAL_SECONDS": "0",
        },
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


def test_reconciler_waits_for_idle_then_verifies_private_state(tmp_path: Path) -> None:
    result, calls, logs = _run_reconcile_harness(
        tmp_path,
        initial_state="Enabled Allow AzureServices",
        active_builds=["1", "0", "0"],
    )

    assert result.returncode == 0, result.stderr
    assert "--public-network-enabled false" in calls
    assert "Waiting for 1 active ACR build(s)" in logs
    assert "ACR private steady state verified" in logs


def test_reconciler_fails_closed_when_builds_never_idle(tmp_path: Path) -> None:
    result, calls, logs = _run_reconcile_harness(
        tmp_path,
        initial_state="Enabled Allow AzureServices",
        active_builds=["1", "1"],
    )

    assert result.returncode == 1
    assert calls == ""
    assert "did not become idle before the restore deadline" in logs


def test_reconciler_defers_when_build_starts_after_idle_check(tmp_path: Path) -> None:
    result, calls, logs = _run_reconcile_harness(
        tmp_path,
        initial_state="Enabled Allow AzureServices",
        active_builds=["0", "1"],
    )

    assert result.returncode == 1
    assert calls == ""
    assert "other ACR build(s) still active" in logs
    assert "private steady state did not become effective" in logs


def test_azure_calls_use_configured_subscription() -> None:
    source = _SCRIPT.read_text()

    assert source.count('subscription_args=(--subscription "$AZURE_SUBSCRIPTION_ID")') == 4
    assert source.count('"${subscription_args[@]}"') == 4


def test_build_workflow_has_immediate_and_independent_restore_paths() -> None:
    build_workflow = (_REPO_ROOT / ".github/workflows/build-images.yml").read_text()
    reconcile_workflow = (
        _REPO_ROOT / ".github/workflows/acr-network-reconcile.yml"
    ).read_text()

    assert "- name: Restore ACR private network policy" in build_workflow
    assert "if: ${{ always() }}" in build_workflow
    assert 'ACR_BUILD_ACCESS_IDLE_ATTEMPTS: "1"' in build_workflow
    assert 'ACR_BUILD_ACCESS_PRIVATE_VERIFY_INTERVAL_SECONDS: "5"' in build_workflow
    assert 'workflows: ["Build Images"]' in reconcile_workflow
    assert "types: [completed]" in reconcile_workflow
    assert 'cron: "17 * * * *"' in reconcile_workflow
    assert 'ACR_BUILD_ACCESS_IDLE_ATTEMPTS: "180"' in reconcile_workflow
    assert 'acr_reconcile_private_steady_state "$ACR_NAME"' in reconcile_workflow
