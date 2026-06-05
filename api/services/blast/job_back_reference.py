"""Record→job back-reference lookup: find a caller's BLAST jobs for an accession.

Responsibility: Owner-scoped scan that matches persisted accession-mode BLAST jobs to a
nuccore accession and projects them to a whitelisted, sanitised row shape.
Edit boundaries: Keep the scan/match/projection logic here; HTTP shaping and the degraded
envelope live in the `/api/blast/jobs/by-accession` route, not in this module.
Key entry points: `find_jobs_for_accession`, `accession_base`.
Risky contracts: Only project the whitelisted fields — never leak the raw payload. Caller
scoping is the route's responsibility (it passes `caller.object_id` as `owner_oid`); this
module trusts that scoping and must not widen it.
Validation: `uv run pytest -q api/tests/test_job_back_reference.py`.
"""

from __future__ import annotations

from typing import Any

from api.services.blast.job_state import _payload_value
from api.services.sanitise import sanitise

# Bounded window of the caller's most-recent jobs to scan for an accession
# match. `list_for_owner` already returns newest-first, so the most relevant
# jobs are always inside this window. Phase 2 (a denormalised column) would
# remove the need to read payloads at all; until then this cap keeps the scan
# cost predictable regardless of how many historical jobs the caller owns.
SCAN_LIMIT = 200


def accession_base(accession: str) -> str:
    """Return the version-stripped, upper-cased accession key.

    Mirrors the precedent in
    ``api/services/blast/web_blast_parity.py`` (``acc.split(".", 1)[0].upper()``)
    so ``NM_000546.6`` and ``nm_000546`` compare equal under ``match=base``.
    """
    return accession.split(".", 1)[0].strip().upper()


def _project_job_row(state: Any, query_metadata: dict[str, Any]) -> dict[str, Any]:
    """Project a matched job state to the whitelisted response row.

    Only the listed fields cross the HTTP boundary; the raw payload never does.
    The database name is sanitised defensively in case a row ever carries a
    blob-URL database that embeds a subscription id.
    """
    payload = state.payload if isinstance(state.payload, dict) else {}
    db = str(getattr(state, "db", None) or _payload_value(payload, "db", "database") or "")
    start = query_metadata.get("query_accession_seq_start")
    stop = query_metadata.get("query_accession_seq_stop")
    return {
        "job_id": str(state.job_id),
        "status": str(getattr(state, "status", "") or ""),
        "phase": str(getattr(state, "phase", None) or getattr(state, "status", "") or ""),
        "database": sanitise(db) if db else "",
        "created_at": getattr(state, "created_at", None),
        "query_accession": str(query_metadata.get("query_accession") or "") or None,
        "seq_start": start if isinstance(start, int) else None,
        "seq_stop": stop if isinstance(stop, int) else None,
        "detail_url": f"/blast/jobs/{state.job_id}",
    }


def find_jobs_for_accession(
    repo: Any,
    owner_oid: str,
    accession: str,
    *,
    match: str = "base",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the caller's accession-mode BLAST jobs for ``accession``, newest first.

    Phase 1 reads payloads for the caller's recent jobs (``include_payload=True``)
    and filters in Python — no schema change required. Only jobs submitted
    through the accession path carry ``query_metadata.query_source ==
    "ncbi_accession"``; paste-mode FASTA submissions do not match (documented
    coverage gap).

    Args:
        repo: jobstate repository exposing ``list_for_owner``.
        owner_oid: the caller's object id; ``list_for_owner`` scopes to it plus
            cluster-shared (``owner_oid=""``) rows. Cluster-shared rows lack
            dashboard accession metadata so they simply will not match.
        accession: the record accession as shown (with or without ``.version``).
        match: ``"base"`` (default) compares the version-stripped accession
            case-insensitively; ``"exact"`` compares the full accession-version.
        limit: maximum rows to return (the caller-facing cap; the route clamps
            it to a hard ceiling before calling).

    Returns:
        A list of whitelisted, sanitised job rows, truncated to ``limit``.
    """
    accession = accession.strip()
    target_base = accession_base(accession)
    target_exact = accession.upper()

    rows = repo.list_for_owner(owner_oid, limit=SCAN_LIMIT, include_payload=True)
    matched: list[dict[str, Any]] = []
    for state in rows:
        if str(getattr(state, "type", "") or "") != "blast":
            continue
        payload = state.payload if isinstance(state.payload, dict) else {}
        query_metadata = payload.get("query_metadata")
        if not isinstance(query_metadata, dict):
            continue
        if query_metadata.get("query_source") != "ncbi_accession":
            continue
        job_accession = str(query_metadata.get("query_accession") or "").strip()
        if not job_accession:
            continue
        if match == "exact":
            if job_accession.upper() != target_exact:
                continue
        else:
            if accession_base(job_accession) != target_base:
                continue
        matched.append(_project_job_row(state, query_metadata))
        if len(matched) >= limit:
            break
    return matched
