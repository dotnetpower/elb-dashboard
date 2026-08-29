"""Guard: control-plane env and platform coordinates stay consistent.

Module summary: `infra/control-plane-env.json` is the single source of truth
for the Container App GUARD/POLICY env toggles. It is read by BOTH
`infra/modules/containerAppControl.bicep` (loadJsonContent, applied on a full
`azd provision` / postprovision deploy) AND `scripts/dev/quick-deploy.sh`
(applied through an exact-container template PATCH, including the GitHub
Actions `deploy.yml` path). Without this file both fast deploy paths patch
images only and silently skip a Bicep guard-default change, which is how a
no-RBAC user could still load the dashboard after an apparent redeploy.
The same fast path also backfills non-secret platform coordinates required by
runtime maintenance, including `PLATFORM_ACR_NAME` and `STORAGE_ACCOUNT_NAME`
on the four Bicep-owned runtime sidecars.

This test fails loudly when the file is malformed, when a guard key Bicep
references disappears, or when the security-critical default
`ENFORCE_DASHBOARD_RBAC` is flipped away from `"true"` without an intentional
edit here.

Responsibility: Pure file-content invariants — no Azure access, no FastAPI app.
Edit boundaries: Only asserts the JSON shape + that Bicep references every key.
    The deploy wiring lives in the Bicep module and the shell script.
Key entry points: `test_*`.
Risky contracts: The JSON keys and the `controlPlaneEnv.<sidecar>.<KEY>`
    references in the Bicep module must stay in lockstep; this test cross-checks
    them so a rename in one place fails CI instead of drifting silently. Core
    coordinate backfills must match Bicep sidecar ownership and remain
    non-secret.
Validation: `uv run pytest -q api/tests/test_control_plane_env.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JSON_PATH = _REPO_ROOT / "infra" / "control-plane-env.json"
_BICEP_PATH = _REPO_ROOT / "infra" / "modules" / "containerAppControl.bicep"
_QUICK_DEPLOY_PATH = _REPO_ROOT / "scripts" / "dev" / "quick-deploy.sh"
_POSTPROVISION_PATH = _REPO_ROOT / "scripts" / "dev" / "postprovision.sh"
_SERVICE_BUS_UI_PATH = (
    _REPO_ROOT / "web" / "src" / "components" / "settings" / "sections" / "ServiceBusSection.tsx"
)


def _load() -> dict[str, dict[str, str]]:
    return json.loads(_JSON_PATH.read_text(encoding="utf-8"))


def test_json_exists_and_parses() -> None:
    assert _JSON_PATH.is_file(), f"missing {_JSON_PATH}"
    data = _load()
    assert isinstance(data, dict)


def test_expected_sidecars_present() -> None:
    data = _load()
    for sidecar in ("api", "worker", "beat"):
        assert sidecar in data, f"sidecar '{sidecar}' missing from {_JSON_PATH.name}"
        assert isinstance(data[sidecar], dict)


def test_all_guard_values_are_strings() -> None:
    data = _load()
    for sidecar, section in data.items():
        if sidecar.startswith("_"):
            continue  # `_comment` documentation key
        for key, value in section.items():
            assert isinstance(value, str), (
                f"{sidecar}.{key} must be a string (Container App env values are "
                f"always strings); got {type(value).__name__}"
            )


def test_dashboard_rbac_enforced_by_default() -> None:
    """Security-critical: the dashboard entry gate ships ON. Flipping this to
    'false' re-opens the dashboard to any tenant member with zero RBAC, so it
    must be a deliberate edit to this test + the JSON together."""
    data = _load()
    assert data["api"]["ENFORCE_DASHBOARD_RBAC"] == "true"


def _servicebus_notice_output(value: str | None) -> str:
    script = _QUICK_DEPLOY_PATH.read_text(encoding="utf-8")
    start = script.index("servicebus_gate_notice() {")
    end = script.index("\n}\n\n\nrelease_build_number()", start) + len("\n}")
    function = script[start:end]
    command = (
        "_SB_GATE_NOTICE_DONE=false; CONTROL_PLANE_ENV_FILE=/nonexistent; "
        "ts() { printf '%s\\n' \"$*\"; }; "
        f"{function}; servicebus_gate_notice"
    )
    env = os.environ.copy()
    if value is None:
        env.pop("SERVICEBUS_ENABLED", None)
    else:
        env["SERVICEBUS_ENABLED"] = value
    result = subprocess.run(  # noqa: S603 -- repository-controlled function.
        ["/bin/bash", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


@pytest.mark.parametrize("value", [None, "", "true", "yes", "unexpected"])
def test_servicebus_deploy_notice_is_silent_when_config_controls_activation(
    value: str | None,
) -> None:
    assert _servicebus_notice_output(value) == ""


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_servicebus_deploy_notice_only_warns_for_kill_switch(value: str) -> None:
    output = _servicebus_notice_output(value)
    assert "kill switch is active" in output
    assert "forces the saved Settings config OFF" in output
    assert "not pinned ON" not in output


def test_servicebus_ui_describes_three_state_override() -> None:
    ui = _SERVICE_BUS_UI_PATH.read_text(encoding="utf-8")
    assert "saved config controls activation unless SERVICEBUS_ENABLED is explicitly false" in ui
    assert "Both the deployment env switch" not in ui


def test_bicep_references_every_guard_key() -> None:
    """Each guard key in the JSON must be wired into the Bicep via a
    `controlPlaneEnv.<sidecar>.<KEY>` reference, so a key that exists only in
    the JSON (and is therefore never deployed by a full provision) fails CI.

    Exception — per-deployment override keys: a key the deployment can pin via
    an azd-env override (charter §12a Rule 4) is wired through a single
    `empty(param) ? controlPlaneEnv.api.<KEY> : param` var applied to every
    sidecar, so the per-sidecar `controlPlaneEnv.worker/beat.<KEY>` literals are
    intentionally replaced by that var. Such a key is satisfied when both the
    override var and its `controlPlaneEnv.api.<KEY>` fallback are present.
    """
    # key -> the override var that deploys it to all sidecars.
    override_vars = {
        "SERVICEBUS_ENABLED": "effectiveServiceBusEnabled",
        "STORAGE_DATE_LAYOUT_ENABLED": "effectiveStorageDateLayout",
    }
    data = _load()
    bicep = _BICEP_PATH.read_text(encoding="utf-8")
    missing: list[str] = []
    for sidecar, section in data.items():
        if sidecar.startswith("_"):
            continue
        for key in section:
            ref = f"controlPlaneEnv.{sidecar}.{key}"
            if ref in bicep:
                continue
            override_var = override_vars.get(key)
            if override_var and override_var in bicep and f"controlPlaneEnv.api.{key}" in bicep:
                continue
            missing.append(ref)
    assert not missing, f"Bicep is missing references: {missing}"


@pytest.mark.parametrize("sidecar", ["api", "worker", "beat"])
def test_no_secretref_keys_in_guard_json(sidecar: str) -> None:
    """The JSON only carries plain string toggles; secret-backed env (e.g.
    EXEC_TOKEN via secretRef) must never move here; secret lifecycle remains an
    explicit Container App operation rather than a plain JSON value."""
    data = _load()
    assert "EXEC_TOKEN" not in data[sidecar]


def test_shared_keys_match_across_sidecars() -> None:
    """Keys present in more than one sidecar are documented in Bicep as
    "must match the api sidecar" (BLAST_GATE_ENABLED, STRICT_BLUEGREEN). The
    api/worker/beat tasks branch identically on them, so a value that drifts
    between sidecars is a latent split-brain bug. Assert every key shared by
    >1 sidecar carries the same value everywhere it appears."""
    data = _load()
    sections = {name: section for name, section in data.items() if not name.startswith("_")}
    # Collect, per key, the set of (sidecar -> value) where it appears.
    key_values: dict[str, dict[str, str]] = {}
    for sidecar, section in sections.items():
        for key, value in section.items():
            key_values.setdefault(key, {})[sidecar] = value
    drifted = {
        key: by_sidecar
        for key, by_sidecar in key_values.items()
        if len(by_sidecar) > 1 and len(set(by_sidecar.values())) > 1
    }
    assert not drifted, f"shared guard keys drifted across sidecars: {drifted}"


def test_guard_values_have_no_whitespace_or_comma() -> None:
    """Guard values remain simple literal tokens for shell transport.

    All current toggles are `true`/`false`; rejecting whitespace and commas
    prevents an ambiguous future value from crossing the shell boundary.
    """
    data = _load()
    for sidecar, section in data.items():
        if sidecar.startswith("_"):
            continue
        for key, value in section.items():
            assert value == value.strip(), f"{sidecar}.{key} has surrounding whitespace"
            assert " " not in value, f"{sidecar}.{key} value contains a space"
            assert "," not in value, f"{sidecar}.{key} value contains a comma"


def _control_plane_pairs(sidecar: str) -> list[str]:
    script = _QUICK_DEPLOY_PATH.read_text(encoding="utf-8")
    start = script.index("control_plane_env_pairs() {")
    end = script.index("\n}\n\n# Upsert the shared M2M token", start) + len("\n}")
    function = script[start:end]
    command = (
        f"CONTROL_PLANE_ENV_FILE='{_JSON_PATH}'; "
        "ACR_NAME=acrelbdashboardtest; "
        "ACR_LOGIN_SERVER=acrelbdashboardtest.azurecr.io; "
        "STORAGE_ACCOUNT_NAME=stelbdashboardtest; "
        "TAG=v0.3.0-test; "
        "LOG_ANALYTICS_WORKSPACE_ID=648cd0d4-a8b7-41da-a22c-050b5217b153; "
        f"{function}; control_plane_env_pairs '{sidecar}'"
    )
    result = subprocess.run(  # noqa: S603 -- repository-controlled function.
        ["/bin/bash", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def test_quick_deploy_upserts_live_wall_workspace_on_api_only() -> None:
    expected = "LOG_ANALYTICS_WORKSPACE_ID=648cd0d4-a8b7-41da-a22c-050b5217b153"
    assert expected in _control_plane_pairs("api")
    assert expected not in _control_plane_pairs("worker")
    assert expected not in _control_plane_pairs("beat")


def test_quick_deploy_backfills_platform_acr_on_runtime_sidecars() -> None:
    expected = "PLATFORM_ACR_NAME=acrelbdashboardtest"
    for sidecar in ("api", "worker", "beat", "terminal"):
        assert expected in _control_plane_pairs(sidecar)
    assert expected not in _control_plane_pairs("frontend")


def test_quick_deploy_backfills_storage_account_on_runtime_sidecars() -> None:
    expected = "STORAGE_ACCOUNT_NAME=stelbdashboardtest"
    for sidecar in ("api", "worker", "beat", "terminal"):
        assert expected in _control_plane_pairs(sidecar)
    assert expected not in _control_plane_pairs("frontend")


def test_quick_deploy_does_not_overwrite_storage_account_with_empty_value() -> None:
    script = _QUICK_DEPLOY_PATH.read_text(encoding="utf-8")
    start = script.index("control_plane_env_pairs() {")
    end = script.index("\n}\n\n# Upsert the shared M2M token", start) + len("\n}")
    function = script[start:end]
    command = (
        f"CONTROL_PLANE_ENV_FILE='{_JSON_PATH}'; "
        "ACR_NAME=acrelbdashboardtest; "
        f"{function}; control_plane_env_pairs api"
    )
    env = os.environ.copy()
    env.pop("STORAGE_ACCOUNT_NAME", None)
    result = subprocess.run(  # noqa: S603 -- repository-controlled function.
        ["/bin/bash", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert not any(
        line.startswith("STORAGE_ACCOUNT_NAME=") for line in result.stdout.splitlines()
    )


def test_quick_deploy_backfills_prepare_db_image_on_api_only() -> None:
    expected = (
        "PREPARE_DB_AKS_AZCOPY_IMAGE="
        "acrelbdashboardtest.azurecr.io/elb-prepare-db:v0.3.0-test"
    )
    assert expected in _control_plane_pairs("api")
    assert expected not in _control_plane_pairs("worker")
    assert expected not in _control_plane_pairs("beat")


def test_quick_deploy_uses_exact_container_env_patches() -> None:
    script = _QUICK_DEPLOY_PATH.read_text(encoding="utf-8")
    assert script.count("containerapp_patch_container \\") == 2
    assert '--set-env-vars "${_cp_pairs[@]}"' not in script
    assert "Content-Type=application/json" in script
    assert '"If-Match=*"' in script
    assert '"$current_template_hash" == "$template_hash"' in script
    assert '[[ "$verify_status" == "unchanged" ]]' in script
    assert 'CONTAINER_APP_API_VERSION="${CONTAINER_APP_API_VERSION:-2026-01-01}"' in script
    assert "deadline=$((SECONDS + 300))" in script
    assert "join(' ', [properties.provisioningState, properties.runningState])" in script
    assert "control-plane env file missing" in script
    assert "failed to upsert Container App secret" in script
    assert "could not resolve immutable digest" in script
    assert "patching with the mutable tag" not in script
    assert 'if [[ "$SIDECAR" == "api" ]]; then' in script
    # Single-api and all-sidecar/GHA build paths both produce the matching
    # prepare image before either path can inject its tag into the api env.
    assert script.count('--image "elb-prepare-db:${TAG}"') == 2
    assert '["elb-prepare-db"]=$PID_PREPARE_DB' in script


def _exact_env_patch_result(
    tmp_path: Path,
    *,
    template_drift: bool,
) -> subprocess.CompletedProcess[str]:
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    drifted = tmp_path / "drifted.json"
    rest_log = tmp_path / "rest.log"
    before.write_text(
        json.dumps(
            {
                "id": (
                    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/containerApps/ca"
                ),
                "properties": {
                    "template": {
                        "containers": [
                            {"name": "api", "image": "api:v1", "env": []},
                            {"name": "worker", "image": "api:v1", "env": []},
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    after.write_text(
        json.dumps(
            {
                "id": (
                    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/containerApps/ca"
                ),
                "properties": {
                    "template": {
                        "containers": [
                            {"name": "api", "image": "api:v1", "env": []},
                            {
                                "name": "worker",
                                "image": "api:v1",
                                "env": [
                                    {
                                        "name": "PLATFORM_ACR_NAME",
                                        "value": "acrelbdashboardtest",
                                    }
                                ],
                            },
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    drifted.write_text(
        json.dumps(
            {
                "id": (
                    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/containerApps/ca"
                ),
                "properties": {
                    "template": {
                        "containers": [
                            {"name": "api", "image": "api:v1", "env": []},
                            {"name": "worker", "image": "api:concurrent", "env": []},
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    script = _QUICK_DEPLOY_PATH.read_text(encoding="utf-8")
    start = script.index("containerapp_patch_container() {")
    marker = (
        "\n}\n\n# "
        "---------------------------------------------------------------------------"
        "\n# Per-sidecar"
    )
    end = script.index(marker, start) + len("\n}")
    function = script[start:end]
    command = f"""
set -Eeuo pipefail
REPO_ROOT={_REPO_ROOT!s}
CONTAINER_APP_NAME=ca
AZURE_RESOURCE_GROUP=rg
AZURE_SUBSCRIPTION_ID=sub
CONTAINER_APP_API_VERSION=2026-01-01
BEFORE={before!s}
AFTER={after!s}
DRIFTED={drifted!s}
REST_LOG={rest_log!s}
ACTIVE_FILE={tmp_path / "active-revision.txt"!s}
TEMPLATE_DRIFT={str(template_drift).lower()}
get_count=0
patched=0
ts() {{ printf '%s\n' "$*"; }}
die() {{ printf 'ERROR: %s\n' "$*" >&2; return 1; }}
timeout() {{ shift; "$@"; }}
az() {{
  if [[ "$1 $2" == "containerapp show" ]]; then
        if [[ " $* " == *" --query properties.latestRevisionName "* ]]; then
            cat "$ACTIVE_FILE"
        fi
  elif [[ "$1" == "rest" ]]; then
        if [[ " $* " == *" --method get "* ]]; then
            if (( patched )); then
                cat "$AFTER"
            elif (( get_count == 0 )); then
                cat "$BEFORE"
            elif $TEMPLATE_DRIFT; then
                cat "$DRIFTED"
            else
                cat "$BEFORE"
            fi
            get_count=$((get_count + 1))
        elif [[ " $* " == *" --method patch "* ]]; then
            printf '%s\n' "$*" > "$REST_LOG"
            patched=1
        fi
  elif [[ "$1 $2 $3" == "containerapp revision show" ]]; then
        previous=""
        for arg in "$@"; do
            if [[ "$previous" == "--revision" ]]; then
                printf '%s\n' "$arg" > "$ACTIVE_FILE"
            fi
            previous="$arg"
        done
    printf 'Provisioned RunningAtMaxScale\n'
  else
    printf 'unexpected az invocation: %s\n' "$*" >&2
    return 99
  fi
}}
{function}
containerapp_patch_container worker api:v1 "" "" PLATFORM_ACR_NAME=acrelbdashboardtest
"""
    return subprocess.run(  # noqa: S603 -- repository-controlled function and fixture paths.
        ["/bin/bash", "-c", command],
        capture_output=True,
        text=True,
        check=False,
    )


def test_exact_container_env_shell_flow_patches_and_verifies(tmp_path: Path) -> None:
    result = _exact_env_patch_result(tmp_path, template_drift=False)

    assert result.returncode == 0, result.stderr
    assert "image/resources/env converged" in result.stdout
    rest_call = (tmp_path / "rest.log").read_text(encoding="utf-8")
    assert "api-version=2026-01-01" in rest_call
    assert "Content-Type=application/json" in rest_call
    assert "If-Match=*" in rest_call


def test_exact_container_env_shell_flow_stops_on_template_drift(tmp_path: Path) -> None:
    result = _exact_env_patch_result(tmp_path, template_drift=True)

    assert result.returncode != 0
    assert "template changed" in result.stderr
    assert not (tmp_path / "rest.log").exists()


def _resolve_digest_result(
    tmp_path: Path,
    *,
    succeeds_on: int,
) -> subprocess.CompletedProcess[str]:
    script = _QUICK_DEPLOY_PATH.read_text(encoding="utf-8")
    start = script.index("resolve_image_digest() {")
    marker = (
        "\n}\n\n# "
        "---------------------------------------------------------------------------"
        "\n# acr_prune"
    )
    end = script.index(marker, start) + len("\n}")
    function = script[start:end]
    counter = tmp_path / "digest-attempts.txt"
    command = f"""
set -Eeuo pipefail
COUNTER={counter!s}
SUCCEEDS_ON={succeeds_on}
printf '0' > "$COUNTER"
sleep() {{ :; }}
timeout() {{ shift; "$@"; }}
az() {{
  count="$(cat "$COUNTER")"
  count=$((count + 1))
  printf '%s' "$count" > "$COUNTER"
  if (( count >= SUCCEEDS_ON )); then
    printf 'sha256:abcdef\n'
  fi
}}
{function}
resolve_image_digest acrelb.azurecr.io/elb-api:latest-main
"""
    return subprocess.run(  # noqa: S603 -- repository-controlled function.
        ["/bin/bash", "-c", command],
        capture_output=True,
        text=True,
        check=False,
    )


def test_digest_resolution_retries_then_returns_immutable_ref(tmp_path: Path) -> None:
    result = _resolve_digest_result(tmp_path, succeeds_on=3)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "acrelb.azurecr.io/elb-api@sha256:abcdef"
    assert (tmp_path / "digest-attempts.txt").read_text(encoding="utf-8") == "3"


def test_digest_resolution_fails_instead_of_returning_mutable_tag(tmp_path: Path) -> None:
    result = _resolve_digest_result(tmp_path, succeeds_on=99)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "could not resolve immutable digest" in result.stderr
    assert (tmp_path / "digest-attempts.txt").read_text(encoding="utf-8") == "3"


def test_postprovision_prefers_container_environment_workspace() -> None:
    script = _POSTPROVISION_PATH.read_text(encoding="utf-8")
    assert "resolve_live_wall_workspace_customer_id" in script
    assert 'LOG_ANALYTICS_WORKSPACE_ID_VAL="$(resolve_live_wall_workspace_customer_id' in script
