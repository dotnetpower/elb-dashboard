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

import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from api.services.blast.citation import build_citation
from api.services.blast.provenance import build_blast_provenance
from api.services.blast.submit_payload import canonical_submit_snapshot
from api.services.blast.workflow_export import (
    SUPPORTED_WORKFLOW_FORMATS,
    MissingDatabaseError,
    render_workflow_export,
)
from api.services.sanitise import sanitise

_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "bearer",
        "client_secret",
        "credential",
        "external_correlation_id",
        "idempotency_key",
        "password",
        "query_data",
        "query_fasta",
        "query_sequence",
        "raw_query",
        "refresh_token",
        "sas",
        "sas_token",
        "secret",
        "sig",
        "signature",
        "token",
    }
)
_STORAGE_HOST_MARKERS = (".blob.core.", ".dfs.core.", ".file.core.")
_FASTA_SEQUENCE_RE = re.compile(r"^[A-Za-z*.-]+$")


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


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().casefold().replace("-", "_")
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
        or ("query" in normalized and "sequence" in normalized)
    )


def _portable_string(value: str) -> str:
    lines = [line.strip() for line in value.strip().splitlines() if line.strip()]
    if (
        len(lines) >= 2
        and lines[0].startswith(">")
        and all(_FASTA_SEQUENCE_RE.fullmatch(line) for line in lines[1:])
    ):
        return "<query-sequence-redacted>"
    cleaned = sanitise(value, mask_subscription_ids=False)
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return cleaned
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme in {"http", "https"} and any(
        marker in hostname for marker in _STORAGE_HOST_MARKERS
    ):
        return parsed.path.lstrip("/")
    return cleaned


def _portable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _portable_value(item)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [_portable_value(item) for item in value]
    if isinstance(value, tuple):
        return [_portable_value(item) for item in value]
    if isinstance(value, str):
        return _portable_string(value)
    return deepcopy(value)


def _portable_mapping(value: object) -> dict[str, Any]:
    portable = _portable_value(value)
    return portable if isinstance(portable, dict) else {}


def _safe_submit_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("canonical_request")
    snapshot = raw if isinstance(raw, dict) else canonical_submit_snapshot(payload)
    return _portable_mapping(snapshot)


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
    raw_or_built_provenance = (
        raw_provenance
        if isinstance(raw_provenance, dict)
        else build_blast_provenance(job_id=job_id, payload=payload)
    )
    provenance = _portable_mapping(raw_or_built_provenance)
    job_title = getattr(state, "job_title", None) or payload.get("job_title")
    safe_job_title = _portable_string(job_title) if isinstance(job_title, str) else None
    citation_bundle = build_citation(
        job_id=job_id,
        provenance=provenance,
        job_title=safe_job_title,
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
            "job_title": safe_job_title,
            "program": _portable_string(
                str(getattr(state, "program", "") or snapshot.get("program") or "")
            ),
            "database": _portable_string(
                str(getattr(state, "db", "") or snapshot.get("database") or "")
            ),
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
        result_manifest=_portable_mapping(result_manifest)
        if isinstance(result_manifest, dict)
        else None,
        availability={
            "result_manifest": isinstance(result_manifest, dict),
            "workflow_exports": bool(workflow_exports),
        },
    )
