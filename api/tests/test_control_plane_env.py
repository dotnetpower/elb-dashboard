"""Guard: control-plane env and platform coordinates stay consistent.

Module summary: `infra/control-plane-env.json` is the single source of truth
for the Container App GUARD/POLICY env toggles. It is read by BOTH
`infra/modules/containerAppControl.bicep` (loadJsonContent, applied on a full
`azd provision` / postprovision deploy) AND `scripts/dev/quick-deploy.sh`
(applied as `--set-env-vars` on every api/worker/beat PATCH, including the
GitHub Actions `deploy.yml` path). Without this file both fast deploy paths
patch images only and silently skip a Bicep guard-default change, which is how
a no-RBAC user could still load the dashboard after an apparent redeploy.
The same fast path also backfills non-secret platform coordinates required by
runtime maintenance, including `PLATFORM_ACR_NAME` on the four Bicep-owned
runtime sidecars.

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
    _REPO_ROOT
    / "web"
    / "src"
    / "components"
    / "settings"
    / "sections"
    / "ServiceBusSection.tsx"
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
            if (
                override_var
                and override_var in bicep
                and f"controlPlaneEnv.api.{key}" in bicep
            ):
                continue
            missing.append(ref)
    assert not missing, f"Bicep is missing references: {missing}"


@pytest.mark.parametrize("sidecar", ["api", "worker", "beat"])
def test_no_secretref_keys_in_guard_json(sidecar: str) -> None:
    """The JSON only carries plain string toggles; secret-backed env (e.g.
    EXEC_TOKEN via secretRef) must never move here — quick-deploy applies these
    as literal `--set-env-vars`, which would expose a secret value."""
    data = _load()
    assert "EXEC_TOKEN" not in data[sidecar]


def test_shared_keys_match_across_sidecars() -> None:
    """Keys present in more than one sidecar are documented in Bicep as
    "must match the api sidecar" (BLAST_GATE_ENABLED, STRICT_BLUEGREEN). The
    api/worker/beat tasks branch identically on them, so a value that drifts
    between sidecars is a latent split-brain bug. Assert every key shared by
    >1 sidecar carries the same value everywhere it appears."""
    data = _load()
    sections = {
        name: section
        for name, section in data.items()
        if not name.startswith("_")
    }
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
    """quick-deploy applies each pair as a literal `KEY=VALUE` token to
    `az containerapp update --set-env-vars`. A value containing whitespace or a
    comma would be mis-split by the CLI, silently truncating or mangling the
    env var. All current toggles are `true`/`false`; this guards a future
    value that would break the shell wiring."""
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


def test_postprovision_prefers_container_environment_workspace() -> None:
    script = _POSTPROVISION_PATH.read_text(encoding="utf-8")
    assert "resolve_live_wall_workspace_customer_id" in script
    assert 'LOG_ANALYTICS_WORKSPACE_ID_VAL="$(resolve_live_wall_workspace_customer_id' in script


