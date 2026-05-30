"""Contract test: every sibling ``/v1/ready`` upstream code mapped in the
dashboard has a matching SPA remediation hint, and vice versa.

Responsibility: Guard against drift between the three places ``upstream_code``
values are referenced: sibling ``docker-openapi/app/main.py``, dashboard
``api/services/blast/submit_gates.py::OPENAPI_UPSTREAM_ACTIONS``, and SPA
``web/src/api/client.ts::OPENAPI_UPSTREAM_HINTS``.
Edit boundaries: Pure read-only filesystem inspection of the SPA file plus a
direct import of the dashboard mapping. Do not import the sibling repo \u2014 it
is not a Python package dependency.
Key entry points: ``test_dashboard_codes_have_spa_hints``,
``test_spa_hints_have_dashboard_codes``.
Risky contracts: The SPA-side hint table is detected by a tiny regex over the
literal source; if the file is renamed or reformatted, update the path /
pattern here.
Validation: ``uv run pytest -q api/tests/test_openapi_upstream_codes_contract.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from api.services.blast.submit_gates import openapi_known_upstream_codes

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPA_CLIENT = _REPO_ROOT / "web" / "src" / "api" / "client.ts"
# Capture the body of ``const OPENAPI_UPSTREAM_HINTS: Record<...> = { ... };``
# so we can pull the keys out cleanly even if the table grows.
_HINTS_BLOCK_RE = re.compile(
    r"const OPENAPI_UPSTREAM_HINTS: Record<[^>]+>\s*=\s*\{(?P<body>[^}]+)\};",
    re.DOTALL,
)
_HINTS_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", re.MULTILINE)


def _spa_hint_codes() -> frozenset[str]:
    text = _SPA_CLIENT.read_text(encoding="utf-8")
    match = _HINTS_BLOCK_RE.search(text)
    if not match:
        pytest.fail(
            "OPENAPI_UPSTREAM_HINTS table not found in web/src/api/client.ts \u2014 "
            "rename / reformat? Update the regex in this test."
        )
    return frozenset(_HINTS_KEY_RE.findall(match.group("body")))


def test_dashboard_codes_have_spa_hints() -> None:
    """Every code the dashboard maps must have an SPA hint entry.

    Otherwise the SPA falls through to the generic "Check AKS cluster health"
    copy and the operator loses the specific action hint the backend already
    knows about.
    """
    dashboard_codes = openapi_known_upstream_codes()
    spa_codes = _spa_hint_codes()
    missing = dashboard_codes - spa_codes
    assert not missing, (
        f"Dashboard maps these upstream codes but the SPA has no hint for them: "
        f"{sorted(missing)}. Add entries to OPENAPI_UPSTREAM_HINTS in "
        f"web/src/api/client.ts."
    )


def test_spa_hints_have_dashboard_codes() -> None:
    """Every SPA hint entry must correspond to a code the dashboard maps.

    A stale entry is harmless at runtime (the backend just never emits that
    code) but accumulates dead code and confuses future readers. Treat as a
    drift signal.
    """
    dashboard_codes = openapi_known_upstream_codes()
    spa_codes = _spa_hint_codes()
    extra = spa_codes - dashboard_codes
    assert not extra, (
        f"SPA OPENAPI_UPSTREAM_HINTS references codes the dashboard does not "
        f"map: {sorted(extra)}. Either drop them from web/src/api/client.ts "
        f"or add them to OPENAPI_UPSTREAM_ACTIONS in "
        f"api/services/blast/submit_gates.py."
    )


# ── Canonical sibling-source contract (critique #20.4) ───────────────────
#
# Hand-curated mirror of the upstream ``code = ...`` literals emitted by
# the sibling ``elastic-blast-azure/docker-openapi/app/main.py`` ``v1_ready``
# handler. When sibling adds a new code (e.g. ``azure_arm_unauthorized``,
# ``cache_failed``, ``node_taint_pressure``), the dashboard's
# ``OPENAPI_UPSTREAM_ACTIONS`` table must learn to remediate it or the SPA
# silently falls through to generic 4xx copy. The test below is the gate.
#
# Source of truth: the ``code = "..."`` assignments inside
# ``docker-openapi/app/main.py::v1_ready`` (lines ~1785-1850 as of sibling
# VERSION=3.7.3). The 429 ``rate_limited`` envelope code is intentionally
# excluded because the dashboard remaps it to the dashboard-only
# ``openapi_ready_rate_limited`` wrapper before the SPA sees it (see
# ``api/services/blast/submit_gates.py`` 429 branch).
#
# WHEN UPDATING THIS LIST you must also extend
# ``OPENAPI_UPSTREAM_ACTIONS`` in ``api/services/blast/submit_gates.py``
# *in the same commit* — otherwise dashboard tests still pass but real
# submits silently lose remediation hints.
KNOWN_SIBLING_NESTED_CODES: frozenset[str] = frozenset(
    {
        "k8s_unreachable",
        "no_workload_nodes",
        "workload_pool_check_failed",
        "openapi_pod_not_ready",
        "openapi_pod_check_failed",
    }
)


def test_dashboard_codes_match_known_sibling_codes() -> None:
    """The dashboard's ``OPENAPI_NESTED_UPSTREAM_CODES`` must equal the
    hand-curated mirror of the sibling's emitted codes.

    Fails on either direction: sibling added a code but dashboard didn't
    learn to remediate it, or dashboard stopped mapping a code the sibling
    still emits. Tracks critique #20.4.
    """
    dashboard_codes = openapi_known_upstream_codes()
    sibling_only = KNOWN_SIBLING_NESTED_CODES - dashboard_codes
    dashboard_only = dashboard_codes - KNOWN_SIBLING_NESTED_CODES
    assert not sibling_only and not dashboard_only, (
        "Sibling-vs-dashboard upstream-code drift:\n"
        f"  Sibling emits but dashboard does not map: {sorted(sibling_only)}\n"
        f"  Dashboard maps but sibling no longer emits: {sorted(dashboard_only)}\n"
        "Update OPENAPI_UPSTREAM_ACTIONS in api/services/blast/submit_gates.py "
        "and KNOWN_SIBLING_NESTED_CODES in this file together (critique #20.4)."
    )

