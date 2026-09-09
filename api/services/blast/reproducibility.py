"""Build portable, secret-free BLAST reproducibility packages.

Responsibility: Assemble existing submit, provenance, citation, workflow, and result-manifest
    metadata into one JSON-serializable package without reading or mutating job state.
Edit boundaries: Pure package construction only; routes own auth and artifact reads, while
    citation/workflow renderers remain authoritative for their formats.
Key entry points: ``build_reproducibility_package``, ``ReproducibilityPackage``.
Risky contracts: Never include raw query sequence, bearer/SAS material, or idempotency and
    correlation identities that could accidentally replay an execution.
Validation: ``uv run pytest -q api/tests/test_blast_reproducibility.py``.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from api.services.blast.citation import build_citation
from api.services.blast.provenance import build_blast_provenance
from api.services.blast.submit_payload import canonical_submit_snapshot
from api.services.blast.workflow_export import (
    SUPPORTED_WORKFLOW_FORMATS,
    MissingDatabaseError,
    render_workflow_export,
)


class WorkflowModule(BaseModel):
    """One self-contained workflow-manager export embedded in the package."""

    filename: str
    media_type: str
    content: str


class ReproducibilityPackage(BaseModel):
    """Versioned portable metadata needed to audit or reproduce one search."""

    schema_version: int = 1
    generated_at: str
    job: dict[str, Any]
    submit_snapshot: dict[str, Any]
    provenance: dict[str, Any]
    citation: dict[str, Any]
    workflow_exports: dict[str, WorkflowModule] = Field(default_factory=dict)
    result_manifest: dict[str, Any] | None = None
    availability: dict[str, bool]
    raw_query_included: bool = False


def _safe_submit_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("canonical_request")
    snapshot = deepcopy(raw) if isinstance(raw, dict) else canonical_submit_snapshot(payload)
    metadata = snapshot.get("metadata")
    if isinstance(metadata, dict):
        # These identify one execution attempt, not its scientific inputs. A
        # portable package must never encourage replay with the same identity.
        metadata.pop("idempotency_key", None)
        metadata.pop("external_correlation_id", None)
    return snapshot


def build_reproducibility_package(
    *,
    state: Any,
    result_manifest: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> ReproducibilityPackage:
    """Assemble one package from an already-authorized job state.

    This function is intentionally pure: the caller supplies an optional
    manifest and owns every Storage/auth decision. Missing legacy metadata is
    represented explicitly instead of failing the export.
    """
    job_id = str(getattr(state, "job_id", "") or "")
    raw_payload = getattr(state, "payload", None)
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    snapshot = _safe_submit_snapshot(payload)

    raw_provenance = payload.get("provenance")
    provenance = (
        deepcopy(raw_provenance)
        if isinstance(raw_provenance, dict)
        else build_blast_provenance(job_id=job_id, payload=payload)
    )
    job_title = getattr(state, "job_title", None) or payload.get("job_title")
    citation_bundle = build_citation(
        job_id=job_id,
        provenance=provenance,
        job_title=job_title if isinstance(job_title, str) else None,
    )

    workflow_exports: dict[str, WorkflowModule] = {}
    for export_format in SUPPORTED_WORKFLOW_FORMATS:
        try:
            export = render_workflow_export(
                job_id=job_id,
                snapshot=snapshot,
                fmt=export_format,
            )
        except MissingDatabaseError:
            break
        workflow_exports[export_format] = WorkflowModule(
            filename=export.filename,
            media_type=export.media_type,
            content=export.content,
        )

    return ReproducibilityPackage(
        generated_at=generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        job={
            "job_id": job_id,
            "job_title": job_title if isinstance(job_title, str) else None,
            "program": str(getattr(state, "program", "") or snapshot.get("program") or ""),
            "database": str(getattr(state, "db", "") or snapshot.get("database") or ""),
            "status": str(getattr(state, "status", "") or ""),
            "created_at": getattr(state, "created_at", None),
            "updated_at": getattr(state, "updated_at", None),
        },
        submit_snapshot=snapshot,
        provenance=provenance,
        citation={
            "text": citation_bundle.text,
            "markdown": citation_bundle.markdown,
            "bibtex": citation_bundle.bibtex,
            "rid": citation_bundle.rid,
        },
        workflow_exports=workflow_exports,
        result_manifest=deepcopy(result_manifest) if isinstance(result_manifest, dict) else None,
        availability={
            "result_manifest": isinstance(result_manifest, dict),
            "workflow_exports": bool(workflow_exports),
        },
    )
