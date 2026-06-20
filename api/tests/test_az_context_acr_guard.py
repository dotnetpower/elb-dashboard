"""Tests for the cross-sub ACR override guard in scripts/dev/az-context.sh.

Responsibility: Lock the safety guard that refuses a deploy when an operator's
    ``ACR_NAME`` override names a registry the ACTIVE subscription does not own
    (the dual-env same-name hazard: ``ca-elb-dashboard`` exists in two subs, so
    an ``az containerapp update`` on a stale active sub would PATCH the wrong
    environment while ``az acr build`` pushed to the named registry).
Edit boundaries: Test module only. Stubs ``az`` / ``azd`` on PATH and runs the
    real ``prepare_deploy_env_from_az_login`` bash function in a subshell.
Key entry points: ``test_acr_override_mismatch_refused``,
    ``test_acr_override_match_passes``.
Risky contracts: Mirrors the guard's exit code (3) and the
    ``ELB_ALLOW_ACR_OVERRIDE_MISMATCH=1`` escape hatch.
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
  *) printf '' ;;
esac
exit 0
"""

# azd returns the SAME sub as `az account show` so the azd-vs-login guard passes
# cleanly and the ACR guard is the only one that can fire.
_FAKE_AZD = """#!/usr/bin/env bash
case "$*" in
  "env get-values") echo 'AZURE_SUBSCRIPTION_ID="active-sub-0000"' ;;
  "env get-name") echo "testenv" ;;
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
