"""Guard the bundled sidecar resource allocation and fast-deploy reconciliation.

Responsibility: Verify that Bicep, the sizing UI, and quick-deploy agree on the
    six sidecar CPU/memory allocations and remain within the Consumption cap.
Edit boundaries: File-content and shell-helper contract checks only; no Azure
    access and no deployment mutation.
Key entry points: ``test_*``.
Risky contracts: The six-container aggregate must keep the exact 1:2 CPU/GiB
    ratio and stay at or below 4 vCPU / 8 GiB. Both quick-deploy PATCH paths
    must obtain resources from the Bicep source of truth rather than preserving
    stale live values or embedding environment-specific constants.
Validation: ``uv run pytest -q api/tests/test_sidecar_resource_contract.py``.
"""

from __future__ import annotations

import re
import subprocess
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BICEP_PATH = _REPO_ROOT / "infra" / "modules" / "containerAppControl.bicep"
_QUICK_DEPLOY_PATH = _REPO_ROOT / "scripts" / "dev" / "quick-deploy.sh"
_SIZING_UI_PATH = (
    _REPO_ROOT / "web" / "src" / "components" / "settings" / "sections" / "SizingSection.tsx"
)
_SIDECARS = ("api", "frontend", "worker", "beat", "redis", "terminal")
_EXPECTED = {
    "api": (Decimal("1.0"), Decimal("2.0")),
    "frontend": (Decimal("0.25"), Decimal("0.5")),
    "worker": (Decimal("1.75"), Decimal("3.5")),
    "beat": (Decimal("0.25"), Decimal("0.5")),
    "redis": (Decimal("0.25"), Decimal("0.5")),
    "terminal": (Decimal("0.5"), Decimal("1.0")),
}


def _bicep_resources() -> dict[str, tuple[Decimal, Decimal]]:
    text = _BICEP_PATH.read_text(encoding="utf-8")
    resources: dict[str, tuple[Decimal, Decimal]] = {}
    for name in _SIDECARS:
        match = re.search(
            rf"name:\s*'{re.escape(name)}'.*?resources:\s*\{{\s*"
            rf"cpu:\s*json\('([0-9.]+)'\)\s*memory:\s*'([0-9.]+)Gi'",
            text,
            re.DOTALL,
        )
        assert match is not None, f"missing Bicep resources for {name}"
        resources[name] = (Decimal(match.group(1)), Decimal(match.group(2)))
    return resources


def _ui_resources() -> dict[str, tuple[Decimal, Decimal]]:
    text = _SIZING_UI_PATH.read_text(encoding="utf-8")
    resources: dict[str, tuple[Decimal, Decimal]] = {}
    for name in _SIDECARS:
        match = re.search(
            rf"^\s*{re.escape(name)}:\s*\{{\s*cpu:\s*([0-9.]+),\s*"
            rf"memoryGi:\s*([0-9.]+)\s*\}},",
            text,
            re.MULTILINE,
        )
        assert match is not None, f"missing sizing UI resources for {name}"
        resources[name] = (Decimal(match.group(1)), Decimal(match.group(2)))
    return resources


def _quick_deploy_resource_function(script: str) -> str:
    start = script.index("container_desired_resources() {")
    end_marker = (
        "\n}\n\n# ---------------------------------------------------------------------------"
    )
    end = script.index(end_marker, start) + len("\n}")
    return script[start:end]


def test_bicep_allocation_reaches_valid_consumption_cap() -> None:
    resources = _bicep_resources()
    assert resources == _EXPECTED

    total_cpu = sum((cpu for cpu, _memory in resources.values()), Decimal())
    total_memory = sum((memory for _cpu, memory in resources.values()), Decimal())

    assert total_cpu == Decimal("4.0")
    assert total_memory == Decimal("8.0")
    assert total_memory == total_cpu * 2
    for name, (cpu, memory) in resources.items():
        assert memory == cpu * 2, f"{name} violates the 1 vCPU : 2 GiB ratio"
        assert cpu % Decimal("0.25") == 0, f"{name} CPU is not a 0.25 increment"
        assert memory % Decimal("0.5") == 0, f"{name} memory is not a 0.5Gi increment"


def test_sizing_ui_matches_bicep_allocation() -> None:
    assert _ui_resources() == _bicep_resources()


def test_quick_deploy_reads_worker_resources_from_bicep() -> None:
    script = _QUICK_DEPLOY_PATH.read_text(encoding="utf-8")
    function = _quick_deploy_resource_function(script)
    command = (
        f"CONTROL_PLANE_BICEP_FILE={_BICEP_PATH!s}; {function}; container_desired_resources worker"
    )

    result = subprocess.run(  # noqa: S603 -- repository-controlled function and path.
        ["/bin/bash", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "1.75 3.5Gi"


def test_quick_deploy_reconciles_resources_in_both_patch_paths() -> None:
    script = _QUICK_DEPLOY_PATH.read_text(encoding="utf-8")

    assert script.count('_res="$(container_desired_resources "$tgt")"') == 2
    assert script.count('_res_flags=(--cpu "${_res%% *}" --memory "${_res##* }")') == 2
    assert script.count('${_res_flags[@]+"${_res_flags[@]}"}') >= 6
