"""BLAST provenance bundle construction.

Responsibility: BLAST provenance bundle construction
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `build_blast_provenance`, `query_sha256`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from api.services.blast.submit_payload import canonical_submit_snapshot
from api.services.web_blast_searchsp import database_name_from_path


def build_blast_provenance(
    *,
    job_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a reproducibility bundle for a submitted BLAST job."""
    payload_dict = dict(payload)
    snapshot = payload_dict.get("canonical_request")
    if not isinstance(snapshot, dict):
        snapshot = canonical_submit_snapshot(payload_dict)
    compatibility = payload_dict.get("compatibility_contract")
    if not isinstance(compatibility, dict):
        compatibility = None
    precision = payload_dict.get("precision")
    if not isinstance(precision, dict) and compatibility is not None:
        precision_value = compatibility.get("precision")
        precision = precision_value if isinstance(precision_value, dict) else None
    evidence = compatibility.get("evidence") if isinstance(compatibility, dict) else None
    evidence = evidence if isinstance(evidence, dict) else {}
    database = str(
        snapshot.get("database")
        or payload_dict.get("database")
        or payload_dict.get("db")
        or ""
    )
    query = snapshot.get("query") if isinstance(snapshot.get("query"), dict) else {}
    options = snapshot.get("options") if isinstance(snapshot.get("options"), dict) else {}
    return {
        "schema_version": 1,
        "job_id": job_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "artifact": {
            "container": "results",
            "path": f"{job_id}/provenance.json",
        },
        "blast": {
            "program": snapshot.get("program") or payload_dict.get("program"),
            "version": evidence.get("blast_version") or "unknown",
        },
        "database": {
            "name": database_name_from_path(database),
            "input": database,
            "snapshot": evidence.get("database_snapshot"),
            "evidence": evidence.get("evidence"),
            "search_space": compatibility.get("searchsp") if compatibility else None,
            "search_space_source": compatibility.get("search_space_source")
            if compatibility
            else None,
        },
        "query": query,
        "options": options,
        "compatibility": compatibility,
        "precision": precision,
        "config": {
            "snapshot_status": "pending_task_render",
            "expected_file": "elastic-blast.ini",
        },
    }


def query_sha256(query_fasta: str) -> str:
    return hashlib.sha256(query_fasta.encode("utf-8")).hexdigest()
