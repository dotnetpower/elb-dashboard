"""Protect live Container App manifests during quick-deploy ACR pruning.

Responsibility: Exercise the shell retention helpers with a fake Azure CLI.
Edit boundaries: ACR prune discovery and deletion decisions only; image builds
    and live Azure revision rollout remain outside this test module.
Key entry points: `test_prune_*`.
Risky contracts: Current-template and active-revision manifests must survive
    even when build-only runs push them outside the newest-N retention window.
Validation: `uv run pytest -q api/tests/test_quick_deploy_acr_prune.py`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_QUICK_DEPLOY_PATH = _REPO_ROOT / "scripts" / "dev" / "quick-deploy.sh"


def _shell_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}")
    return script[start:end]


def _run_prune(
    tmp_path: Path,
    *,
    fail_live_lookup: bool = False,
    fail_live_recheck: bool = False,
    tagged_template: bool = False,
    concurrent_live: bool = False,
) -> subprocess.CompletedProcess[str]:
    script = _QUICK_DEPLOY_PATH.read_text(encoding="utf-8")
    functions = "\n\n".join(
        _shell_function(script, name)
        for name in (
            "resolve_image_digest",
            "acr_prune_repo_keep_recent",
            "acr_live_image_refs",
            "acr_live_repo_digests",
            "acr_prune_targets",
        )
    )

    fake_az = tmp_path / "az"
    fake_az.write_text(
        r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_AZ_CALLS"
case "$*" in
  containerapp\ show*)
    [[ "$FAKE_LIVE_LOOKUP_FAIL" == "true" ]] && exit 7
        count=0
        [[ -f "$FAKE_SHOW_COUNT" ]] && count="$(cat "$FAKE_SHOW_COUNT")"
        count=$((count + 1))
        printf '%s\n' "$count" > "$FAKE_SHOW_COUNT"
        [[ "$FAKE_LIVE_RECHECK_FAIL" == "true" && "$count" -gt 1 ]] && exit 7
        if [[ "$FAKE_CONCURRENT_LIVE" == "true" && "$count" -gt 1 ]]; then
            printf '%s\n' 'test.azurecr.io/elb-api@sha256:stale'
            exit 0
        fi
        if [[ "$FAKE_TAGGED_TEMPLATE" == "true" ]]; then
            printf '%s\n' 'test.azurecr.io/elb-api:current'
        else
            printf '%s\n' 'test.azurecr.io/elb-api@sha256:template-live'
        fi
    ;;
  containerapp\ revision\ list*)
    printf '%s\n' 'test.azurecr.io/elb-api@sha256:active-live'
    ;;
  acr\ manifest\ list-metadata*)
        printf '%s\n' sha256:new sha256:active-live sha256:template-live sha256:stale sha256:unused
    ;;
    acr\ manifest\ show-metadata*)
        if [[ "$*" == *":deploy-tag"* ]]; then
            printf '%s\n' sha256:new
        else
            printf '%s\n' sha256:template-live
        fi
        ;;
  acr\ manifest\ delete*)
    printf '%s\n' "$*" >> "$FAKE_AZ_DELETES"
    ;;
  *)
    printf 'unexpected az args: %s\n' "$*" >&2
    exit 2
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_az.chmod(0o755)

    command = (
        "set -euo pipefail\n"
        "ts() { printf '%s\\n' \"$*\"; }\n"
        f"{functions}\n"
        "acr_prune_targets elb-api\n"
    )
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ACR_NAME": "test",
        "ACR_LOGIN_SERVER": "test.azurecr.io",
        "AZURE_RESOURCE_GROUP": "rg-test",
        "CONTAINER_APP_NAME": "ca-test",
        "TAG": "deploy-tag",
        "ELB_ACR_KEEP_IMAGES": "1",
        "NO_BUILD": "false",
        "NO_PRUNE": "false",
        "FAKE_LIVE_LOOKUP_FAIL": str(fail_live_lookup).lower(),
        "FAKE_LIVE_RECHECK_FAIL": str(fail_live_recheck).lower(),
        "FAKE_TAGGED_TEMPLATE": str(tagged_template).lower(),
        "FAKE_CONCURRENT_LIVE": str(concurrent_live).lower(),
        "FAKE_SHOW_COUNT": str(tmp_path / "show-count"),
        "FAKE_AZ_CALLS": str(tmp_path / "az-calls.log"),
        "FAKE_AZ_DELETES": str(tmp_path / "az-deletes.log"),
    }
    return subprocess.run(  # noqa: S603 - executes reviewed repo functions with a fake az.
        ["/bin/bash", "-c", command],
        cwd=_REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_prune_preserves_current_and_active_manifests(tmp_path: Path) -> None:
    result = _run_prune(tmp_path)

    assert result.returncode == 0, result.stderr
    deletes = (tmp_path / "az-deletes.log").read_text(encoding="utf-8")
    assert "elb-api@sha256:stale" in deletes
    assert "elb-api@sha256:unused" in deletes
    assert "sha256:active-live" not in deletes
    assert "sha256:template-live" not in deletes
    assert "elb-api@sha256:new" not in deletes
    assert "kept newest 1 + 2 older live manifest(s)" in result.stdout


def test_prune_resolves_tagged_live_manifest_before_deleting(tmp_path: Path) -> None:
    result = _run_prune(tmp_path, tagged_template=True)

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "az-calls.log").read_text(encoding="utf-8")
    deletes = (tmp_path / "az-deletes.log").read_text(encoding="utf-8")
    assert "acr manifest show-metadata test.azurecr.io/elb-api:deploy-tag" in calls
    assert "acr manifest show-metadata test.azurecr.io/elb-api:current" in calls
    assert "elb-api@sha256:stale" in deletes
    assert "sha256:template-live" not in deletes


def test_prune_rechecks_candidate_that_becomes_live_concurrently(tmp_path: Path) -> None:
    result = _run_prune(tmp_path, concurrent_live=True)
    assert result.returncode == 0, result.stderr

    deletes = (tmp_path / "az-deletes.log").read_text(encoding="utf-8")
    assert "elb-api@sha256:stale" not in deletes
    assert "elb-api@sha256:unused" in deletes


def test_prune_stops_when_deletion_time_recheck_fails(tmp_path: Path) -> None:
    result = _run_prune(tmp_path, fail_live_recheck=True)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "az-deletes.log").exists()
    assert "could not revalidate live images for elb-api" in result.stdout


def test_prune_fails_closed_when_live_references_cannot_be_read(tmp_path: Path) -> None:
    result = _run_prune(tmp_path, fail_live_lookup=True)

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "az-calls.log").read_text(encoding="utf-8")
    assert "acr manifest list-metadata" not in calls
    assert not (tmp_path / "az-deletes.log").exists()
    assert "could not read or resolve live images for elb-api" in result.stdout
