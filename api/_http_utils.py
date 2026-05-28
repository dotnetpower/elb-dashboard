"""Shared HTTP boundary helpers - request/response models, error formatting,.

Responsibility: Shared HTTP boundary helpers - request/response models, error formatting,
Edit boundaries: Keep changes scoped to this module responsibility and update nearby tests.
Key entry points: `_validate_azure_name`, `_validate_subscription_id`, `BlastSubmitRequest`,
`AksProvisionRequest`, `AksLifecycleRequest`, `AcrBuildRequest`
Risky contracts: Keep imports lightweight and preserve existing public contracts.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
_AZURE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]{0,62}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SAFE_STR_RE = re.compile(r"^[a-zA-Z0-9_\-./: @]{0,256}$")


def _validate_azure_name(v: str, label: str) -> str:
    if not _AZURE_NAME_RE.match(v):
        raise ValueError(f"{label} must match [a-zA-Z][a-zA-Z0-9-]{{0,62}}")
    return v


def _validate_subscription_id(v: str) -> str:
    if v and not _UUID_RE.match(v):
        raise ValueError("subscription_id must be a UUID")
    return v


# ---------------------------------------------------------------------------
# Request models — BLAST submit
# ---------------------------------------------------------------------------
class BlastSubmitRequest(BaseModel):
    subscription_id: str = ""
    resource_group: str = Field(..., min_length=1, max_length=90)
    cluster_name: str = Field(..., min_length=1, max_length=63)
    storage_account: str = Field(..., min_length=3, max_length=24)
    program: str = Field(..., pattern=r"^(blastn|blastp|blastx|tblastn|tblastx)$")
    database: str = Field(..., min_length=1, max_length=256)
    query_file: str = Field(..., min_length=1, max_length=1024)
    options: dict[str, Any] | None = None

    @field_validator("subscription_id")
    @classmethod
    def check_subscription_id(cls, v: str) -> str:
        return _validate_subscription_id(v)

    @field_validator("resource_group")
    @classmethod
    def check_resource_group(cls, v: str) -> str:
        return _validate_azure_name(v, "resource_group")

    @field_validator("cluster_name")
    @classmethod
    def check_cluster_name(cls, v: str) -> str:
        return _validate_azure_name(v, "cluster_name")

    @model_validator(mode="after")
    def check_storage_blob_urls(self) -> BlastSubmitRequest:
        from api.services.blast.task_config import validate_storage_blob_reference

        validate_storage_blob_reference(
            storage_account=self.storage_account,
            value=self.database,
            label="database",
            expected_container="blast-db",
        )
        validate_storage_blob_reference(
            storage_account=self.storage_account,
            value=self.query_file,
            label="query_file",
            expected_container="queries",
        )
        return self


# ---------------------------------------------------------------------------
# Request models — AKS provision
# ---------------------------------------------------------------------------
class AksProvisionRequest(BaseModel):
    subscription_id: str = ""
    resource_group: str = Field(..., min_length=1, max_length=90)
    region: str = Field(default="koreacentral", min_length=1, max_length=64)
    cluster_name: str = Field(default="elb-cluster", min_length=1, max_length=63)
    node_sku: str = Field(default="Standard_E32s_v5", min_length=1, max_length=64)
    node_count: int = Field(default=3, ge=1, le=100)
    acr_resource_group: str = ""
    acr_name: str = ""
    storage_resource_group: str = ""
    storage_account: str = ""

    @field_validator("subscription_id")
    @classmethod
    def check_subscription_id(cls, v: str) -> str:
        return _validate_subscription_id(v)

    @field_validator("resource_group", "cluster_name")
    @classmethod
    def check_names(cls, v: str) -> str:
        return _validate_azure_name(v, "name")


# ---------------------------------------------------------------------------
# Request models — AKS lifecycle
# ---------------------------------------------------------------------------
class AksLifecycleRequest(BaseModel):
    subscription_id: str = ""
    resource_group: str = Field(..., min_length=1, max_length=90)
    cluster_name: str = Field(..., min_length=1, max_length=63)

    @field_validator("subscription_id")
    @classmethod
    def check_subscription_id(cls, v: str) -> str:
        return _validate_subscription_id(v)


# ---------------------------------------------------------------------------
# Request models — ACR build
# ---------------------------------------------------------------------------
class AcrBuildRequest(BaseModel):
    subscription_id: str = ""
    resource_group: str = Field(..., min_length=1, max_length=90)
    registry_name: str = Field(..., min_length=5, max_length=50)
    images: list[str] | None = None

    @field_validator("subscription_id")
    @classmethod
    def check_subscription_id(cls, v: str) -> str:
        return _validate_subscription_id(v)


# ---------------------------------------------------------------------------
# Request models — Warmup
# ---------------------------------------------------------------------------
class WarmupRequest(BaseModel):
    subscription_id: str = ""
    resource_group: str = Field(..., min_length=1, max_length=90)
    storage_account: str = Field(..., min_length=3, max_length=24)
    database_name: str = Field(..., min_length=1, max_length=256)


# ---------------------------------------------------------------------------
# Standardised error response
# ---------------------------------------------------------------------------
class ErrorResponse(BaseModel):
    """RFC 7807-inspired error envelope returned by all non-2xx responses."""

    code: str
    message: str
    request_id: str | None = None
    retryable: bool = False
    retry_after_seconds: int | None = None
    details: dict[str, Any] | None = None


class TaskAcceptedResponse(BaseModel):
    """Returned by all mutation endpoints that enqueue a Celery task."""

    id: str  # job_id
    task_id: str  # Celery AsyncResult id
    status: str = "queued"
    status_url: str  # GET /api/tasks/{task_id}


# ---------------------------------------------------------------------------
# OpenAPI response templates for the NCBI/accession integration paths.
#
# FastAPI merges these into the route's `responses` dict so the generated
# OpenAPI schema documents every error code the SPA may receive. Codes are
# advisory — the actual response body is the standard ``ErrorResponse``
# envelope and the runtime code (str slug) is set by the raising route.
# ---------------------------------------------------------------------------
NCBI_LOOKUP_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {
        "model": ErrorResponse,
        "description": (
            "Caller-fixable validation error. Possible `code` values: "
            "`ncbi_accession_invalid` (bad accession or sub-range), "
            "`ncbi_query_too_large` (response exceeds the byte cap — supply "
            "a sub-range)."
        ),
    },
    429: {
        "model": ErrorResponse,
        "description": (
            "Dashboard rate limiter exhausted (`ncbi_rate_limited`). NCBI "
            "itself is healthy; retry after ~1 second."
        ),
    },
    503: {
        "model": ErrorResponse,
        "description": (
            "NCBI E-utilities upstream unavailable (`ncbi_lookup_unavailable`). "
            "Already retried once; safe to retry after ~30 seconds."
        ),
    },
}

BLAST_SUBMIT_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {
        "model": ErrorResponse,
        "description": (
            "Caller-fixable validation error. Possible `code` values: "
            "`validation_error`, `invalid_query_fasta`, "
            "`conflicting_query_sources` (accession + inline FASTA / file / "
            "blob URL set at once), `ncbi_accession_invalid`, "
            "`ncbi_query_too_large`."
        ),
    },
    429: {
        "model": ErrorResponse,
        "description": (
            "Dashboard rate limiter exhausted (`ncbi_rate_limited`) while "
            "resolving the accession. Retry after ~1 second."
        ),
    },
    503: {
        "model": ErrorResponse,
        "description": (
            "NCBI lookup failed (`ncbi_lookup_unavailable`) while resolving "
            "the accession. Retry after ~30 seconds, or submit without an "
            "accession."
        ),
    },
}


def new_job_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
