"""Tests for deploy-target discovery guards in scripts/dev/az-context.sh.

Responsibility: Lock cross-sub ACR refusal and selected azd-environment lookup
    while deployment metadata is discovered from the active Azure subscription.
Edit boundaries: Test module only. Stubs ``az`` / ``azd`` on PATH and runs the
    real ``prepare_deploy_env_from_az_login`` bash function in a subshell.
Key entry points: ``test_*``.
Risky contracts: Mirrors the ACR guard's exit code (3), its explicit escape
    hatch, the supported azd command, and the Live Wall workspace customer GUID.
Validation: ``uv run pytest -q api/tests/test_az_context_acr_guard.py -m subprocess``.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AZ_CONTEXT = _REPO_ROOT / "scripts" / "dev" / "az-context.sh"

_FAKE_AZ = """#!/usr/bin/env bash
# Fake az for the ACR-guard test. The active sub is fixed; `acr list` returns
# whatever FAKE_ACR is set to so the test can drive match/mismatch.
case "$*" in
  "account show --query id -o tsv") echo "active-sub-0000" ;;
  *"acr list"*) echo "$FAKE_ACR" ;;
  *"acr show"*loginServer*) echo "$FAKE_ACR.azurecr.io" ;;
  *"group show"*location*) echo "koreacentral" ;;
    *"containerapp env list"*) echo "cae-elb-test" ;;
    *"containerapp env show"*customerId*) echo "$FAKE_LA_CUSTOMER_ID" ;;
    *"monitor log-analytics workspace show"*) echo "$FAKE_ARM_CUSTOMER_ID" ;;
    *"monitor log-analytics workspace list"*) echo "$FAKE_LIST_CUSTOMER_ID" ;;
  *) printf '' ;;
esac
exit 0
"""

# azd returns the SAME sub as `az account show` so the azd-vs-login guard passes
# cleanly and the ACR guard is the only one that can fire.
_FAKE_AZD = """#!/usr/bin/env bash
case "$*" in
  "env get-values") echo 'AZURE_SUBSCRIPTION_ID="active-sub-0000"' ;;
    "env get-value AZURE_ENV_NAME") echo "testenv" ;;
  *) printf '' ;;
esac
exit 0
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_guard(
    tmp_path: Path,
    *,
    fake_acr: str,
    override: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_exec(bin_dir / "az", _FAKE_AZ)
    _write_exec(bin_dir / "azd", _FAKE_AZD)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_ACR"] = fake_acr
    env["ACR_NAME"] = override
    env["AZURE_RESOURCE_GROUP"] = "rg-elb-dashboard"
    # Decouple from the developer's real azd/az session state.
    env.pop("AZURE_SUBSCRIPTION_ID", None)
    env["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] = "no"
    if extra_env:
        env.update(extra_env)

    script = f"source '{_AZ_CONTEXT}'; prepare_deploy_env_from_az_login"
    return subprocess.run(  # noqa: S603 -- test sources the checked-in az-context.sh
        ["bash", "-c", script],  # noqa: S607 -- bash resolved from PATH in CI/dev
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_acr_override_mismatch_refused(tmp_path: Path) -> None:
    # Operator names the customer registry, but the active sub owns a different
    # one → the deploy would patch the wrong environment → refuse with exit 3.
    proc = _run_guard(
        tmp_path,
        fake_acr="acrelbdashboardmoonchoi",
        override="acrelbdashboardcyutlgcnv3",
    )
    assert proc.returncode == 3, proc.stderr
    assert "does NOT match the active subscription" in proc.stderr


def test_acr_override_mismatch_bypassed_with_flag(tmp_path: Path) -> None:
    proc = _run_guard(
        tmp_path,
        fake_acr="acrelbdashboardmoonchoi",
        override="acrelbdashboardcyutlgcnv3",
        extra_env={"ELB_ALLOW_ACR_OVERRIDE_MISMATCH": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "ACR override mismatch acknowledged" in proc.stderr


def test_acr_override_match_passes(tmp_path: Path) -> None:
    # The active sub owns exactly the named registry → no refusal.
    proc = _run_guard(
        tmp_path,
        fake_acr="acrelbdashboardcyutlgcnv3",
        override="acrelbdashboardcyutlgcnv3",
    )
    assert proc.returncode == 0, proc.stderr
    assert "does NOT match the active subscription" not in proc.stderr


def test_selected_azd_environment_uses_supported_get_value_command(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exec(bin_dir / "azd", _FAKE_AZD)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = subprocess.run(  # noqa: S603 -- test sources the checked-in az-context.sh
        [
            "/bin/bash",
            "-c",
            f"source '{_AZ_CONTEXT}'; _az_context_current_azd_env_name",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "testenv"


def test_discovery_exports_container_environment_workspace_customer_id(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exec(bin_dir / "az", _FAKE_AZ)
    _write_exec(bin_dir / "azd", _FAKE_AZD)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_ACR": "acrelbdashboardcyutlgcnv3",
        "FAKE_LA_CUSTOMER_ID": "648cd0d4-a8b7-41da-a22c-050b5217b153",
        "ACR_NAME": "acrelbdashboardcyutlgcnv3",
        "AZURE_RESOURCE_GROUP": "rg-elb-dashboard",
        "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no",
    }
    env.pop("AZURE_SUBSCRIPTION_ID", None)

    result = subprocess.run(  # noqa: S603 -- repository-controlled script.
        [
            "/bin/bash",
            "-c",
            (
                f"source '{_AZ_CONTEXT}'; prepare_deploy_env_from_az_login >/dev/null; "
                "printf '%s' \"$LOG_ANALYTICS_WORKSPACE_ID\""
            ),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "648cd0d4-a8b7-41da-a22c-050b5217b153"


def _run_workspace_resolver(
    tmp_path: Path,
    *,
    candidate: str,
    environment_customer_id: str = "",
    arm_customer_id: str = "",
    list_customer_id: str = "",
) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exec(bin_dir / "az", _FAKE_AZ)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LA_CUSTOMER_ID": environment_customer_id,
        "FAKE_ARM_CUSTOMER_ID": arm_customer_id,
        "FAKE_LIST_CUSTOMER_ID": list_customer_id,
    }
    command = (
        f"source '{_AZ_CONTEXT}'; "
        f"resolve_live_wall_workspace_customer_id '{candidate}' '' 'rg-elb' 'sub-1'"
    )
    result = subprocess.run(  # noqa: S603 -- repository-controlled function.
        ["/bin/bash", "-c", command],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_workspace_resolver_converts_legacy_arm_id(tmp_path: Path) -> None:
    result = _run_workspace_resolver(
        tmp_path,
        candidate=(
            "/subscriptions/sub-1/resourceGroups/rg-elb/providers/"
            "Microsoft.OperationalInsights/workspaces/log-elb"
        ),
        arm_customer_id="648cd0d4-a8b7-41da-a22c-050b5217b153",
    )

    assert result == "648cd0d4-a8b7-41da-a22c-050b5217b153"


def test_workspace_resolver_falls_back_to_current_resource_group(
    tmp_path: Path,
) -> None:
    result = _run_workspace_resolver(
        tmp_path,
        candidate="",
        list_customer_id="648cd0d4-a8b7-41da-a22c-050b5217b153",
    )

    assert result == "648cd0d4-a8b7-41da-a22c-050b5217b153"
