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
