"""Evidence registry validation for Web BLAST compatibility entries.

Responsibility: Evidence registry validation for Web BLAST compatibility entries
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `evidence_registry_matrix`, `validate_evidence_registry`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

from typing import Any

from api.services.web_blast_searchsp import WEB_BLAST_SEARCHSP_DEFAULTS

_REQUIRED_EVIDENCE_FIELDS = frozenset(
    {
        "db_name",
        "value",
        "scope",
        "evidence",
        "blast_version",
        "database_snapshot",
        "option_scope",
        "revalidate_when",
    }
)


def evidence_registry_matrix() -> list[dict[str, Any]]:
    return [entry.as_dict() for entry in WEB_BLAST_SEARCHSP_DEFAULTS.values()]


def validate_evidence_registry() -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for name, entry in WEB_BLAST_SEARCHSP_DEFAULTS.items():
        payload = entry.as_dict()
        missing = sorted(field for field in _REQUIRED_EVIDENCE_FIELDS if not payload.get(field))
        if missing:
            failures.append({"db_name": name, "missing": missing})
    return failures
