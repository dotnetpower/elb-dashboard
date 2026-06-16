"""External ElasticBLAST API facade.

Responsibility: External ElasticBLAST API facade
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `ExternalBlastOptions`, `ExternalBlastSubmitRequest`,
`submit_external_blast_job`, `list_external_blast_jobs`, `get_external_blast_job`,
`list_external_blast_job_events`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from api.auth import CallerIdentity, require_caller
from api.services import external_blast
from api.services.blast.submit_payload import (
    canonical_submit_metadata,
    canonical_submit_snapshot,
    resolve_sharded_db_resource_profile,
    submit_contracts,
)
from api.services.sanitise import redact_oid

router = APIRouter(prefix="/api/v1/elastic-blast", tags=["external-elastic-blast"])
LOGGER = logging.getLogger(__name__)
MAX_QUERY_FASTA_CHARS = 10_000_000
_REQUIRE_CALLER = Depends(require_caller)


class ExternalBlastOptions(BaseModel):
    outfmt: Literal[5] = Field(5, description="Fixed to BLAST XML format 5")
    word_size: int = Field(28, ge=1)
    dust: bool = Field(True)
    evalue: float = Field(
        0.05,
        gt=0,
        description=(
            "Expect-value threshold. Defaults to 0.05 to match the NCBI Web "
            "BLAST megablast default and the dashboard submit form."
        ),
    )
    max_target_seqs: int = Field(500, ge=1)

    @field_validator("outfmt", mode="before")
    @classmethod
    def _coerce_outfmt(cls, value: Any) -> Any:
        """Accept the documented string form ``"5"`` as well as int ``5``.

        The OpenAPI ``/v1/jobs`` contract (and the dashboard's own API Reference
        examples in ``web/src/pages/apiReference/spec.ts``) document ``outfmt``
        as the JSON string ``"5"``, and the sibling plane accepts it. Without
        this coercion a Service Bus producer or API caller copying the
        documented example verbatim was rejected — and on the Service Bus path
        that meant the message was dead-lettered. Coerce the string form to the
        int the ``Literal[5]`` expects; any non-``5`` value (e.g. ``"6"``/``6``)
        still fails validation so the XML-only result pipeline contract holds.
        """
        if isinstance(value, str) and value.strip() == "5":
            return 5
        return value


class ExternalBlastSubmitRequest(BaseModel):
    query_fasta: str = Field(..., min_length=1, max_length=MAX_QUERY_FASTA_CHARS)
    db: str = Field(..., min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._/-]+$")
    program: Literal[
        "blastn",
        "blastp",
        "blastx",
        "psiblast",
        "rpsblast",
        "rpstblastn",
        "tblastn",
        "tblastx",
    ] = Field("blastn")
    # NCBI taxonomy ids are positive integers (the root tax tree starts at 1).
    # A 0 / negative value is never a valid organism filter, so reject it at the
    # boundary instead of forwarding a nonsensical -taxids/-negative_taxids arg
    # to the sibling (which would either error mid-run or silently filter out
    # everything).
    taxid: int | None = Field(None, ge=1, le=2_147_483_647)
    is_inclusive: bool | None = None
    options: ExternalBlastOptions = Field(default_factory=ExternalBlastOptions)  # type: ignore[arg-type]
    priority: int = Field(50, ge=0, le=100)
    batch_len: int | None = Field(None, ge=1, le=1_000_000_000)
    idempotency_key: str | None = Field(None, min_length=1, max_length=256)
    external_correlation_id: str | None = Field(
        None,
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    resource_profile: str = Field(
        "standard", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    )
    subscription_id: str | None = Field(None, min_length=1, max_length=64)
    resource_group: str | None = Field(None, min_length=1, max_length=120)
    cluster_name: str | None = Field(None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_query_and_taxonomy(self) -> ExternalBlastSubmitRequest:
        from api.services.query_metadata import parse_fasta_metadata

        # Reject path-traversal segments in the db name at the boundary. The
        # ``db`` pattern allows ``.`` and ``/`` (legitimate for sharded prefixes
        # like ``10shards/core_nt_shard_``), which also lets ``..`` through.
        # Azure Blob storage does not resolve ``..`` (flat namespace) and the
        # sibling rejects it too, but that rejection only happens once the
        # upstream is reachable — so a ``..`` db returned 503 locally and 400
        # in-cluster. Fail fast and deterministically with 422 here instead, so
        # an invalid db never depends on upstream reachability.
        if ".." in self.db.split("/"):
            raise ValueError("db must not contain '..' path segments")

        try:
            parse_fasta_metadata(self.query_fasta)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        if self.taxid is None and self.is_inclusive is not None:
            raise ValueError("is_inclusive requires taxid")
        if self.taxid is not None and self.is_inclusive is None:
            self.is_inclusive = True
        return self


class BlastV1Options(BaseModel):
    """Free-form sibling ``/v1/jobs`` BLAST options (multi-token ``outfmt``).

    Mirrors the sibling ``BlastOptions``. Unlike ``ExternalBlastOptions`` (which
    pins ``outfmt`` to ``5`` for the XML→FASTA pipeline), ``outfmt`` here is a
    free string so a caller can request a tabular multi-token layout such as
    ``"7 std staxids sstrand qseq sseq"``. ``extra`` carries raw CLI flags.
    """

    evalue: float | None = Field(None, gt=0)
    max_target_seqs: int | None = Field(None, ge=1)
    outfmt: str | None = Field(None, max_length=512)
    extra: str | None = Field(None, max_length=2048)


class ExternalBlastV1Request(BaseModel):
    """Dashboard mirror of the sibling ``JobSubmitRequest`` (Mode B inline FASTA)
    used by the Service Bus multi-token path.

    The Service Bus consumer routes a message carrying ``blast_options`` to the
    sibling ``POST /v1/jobs`` (free-form options) instead of
    ``/api/v1/elastic-blast/submit`` (XML-locked). Validating here means a
    malformed tabular ``outfmt`` is rejected at submit time rather than failing
    the shard-merge finalizer minutes later: the result merge re-ranks shard
    hits by ``evalue`` / ``bitscore`` *by name*, so a tabular layout missing
    either cannot be merged.
    """

    query_fasta: str = Field(..., min_length=1, max_length=MAX_QUERY_FASTA_CHARS)
    db: str = Field(..., min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._/-]+$")
    program: Literal[
        "blastn",
        "blastp",
        "blastx",
        "psiblast",
        "rpsblast",
        "rpstblastn",
        "tblastn",
        "tblastx",
    ] = Field("blastn")
    taxid: int | None = Field(None, ge=1, le=2_147_483_647)
    is_inclusive: bool | None = None
    blast_options: BlastV1Options = Field(default_factory=BlastV1Options)  # type: ignore[arg-type]
    priority: int = Field(50, ge=0, le=100)
    batch_len: int | None = Field(None, ge=1, le=1_000_000_000)
    idempotency_key: str | None = Field(None, min_length=1, max_length=256)
    resource_profile: str = Field(
        "standard", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    )
    external_correlation_id: str | None = Field(
        None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    subscription_id: str | None = Field(None, min_length=1, max_length=64)
    resource_group: str | None = Field(None, min_length=1, max_length=120)
    cluster_name: str | None = Field(None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_query_taxonomy_and_outfmt(self) -> ExternalBlastV1Request:
        from api.services.query_metadata import parse_fasta_metadata

        if ".." in self.db.split("/"):
            raise ValueError("db must not contain '..' path segments")
        try:
            parse_fasta_metadata(self.query_fasta)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if self.taxid is None and self.is_inclusive is not None:
            raise ValueError("is_inclusive requires taxid")
        if self.taxid is not None and self.is_inclusive is None:
            self.is_inclusive = True

        # A sharded DB (e.g. core_nt) runs the shard-merge finalizer, which
        # re-ranks by evalue + bitscore resolved by NAME. Reject a tabular
        # layout that omits either so the failure surfaces now, not minutes
        # later in the merge. XML (outfmt 5) and a bare numeric code merge fine.
        outfmt = self.blast_options.outfmt
        if outfmt is not None and str(outfmt).strip():
            from api.services.sharding_precision import merge_format_for_outfmt

            if merge_format_for_outfmt(outfmt) is None:
                raise ValueError(
                    "blast_options.outfmt is not shard-merge compatible: a tabular "
                    "layout must include both evalue and bitscore (use 'std' or list "
                    "them explicitly)"
                )
        return self



_QUEUED_STATUSES = frozenset({"accepted", "created", "pending", "queued", "scheduled"})
_RUNNING_STATUSES = frozenset(
    {
        "dispatching",
        "finalizing",
        "in_progress",
        "inprogress",
        "reducing",
        "running",
        "splitting",
        "submitted",
        "submitting",
    }
)
_SUCCESS_STATUSES = frozenset({"complete", "completed", "success", "succeeded"})
_FAILED_STATUSES = frozenset({"canceled", "cancelled", "error", "failed", "failure", "timeout"})


def _public_status(value: Any, *, default: str = "queued") -> str:
    status = str(value or "").strip().casefold()
    if status in _QUEUED_STATUSES:
        return "queued"
    if status in _RUNNING_STATUSES:
        return "running"
    if status in _SUCCESS_STATUSES:
        return "success"
    if status in _FAILED_STATUSES:
        return "failed"
    return default


def _normalise_result_file(item: dict[str, Any], index: int) -> dict[str, Any]:
    filename = str(item.get("filename") or item.get("name") or f"blast_result_{index + 1}.xml")
    file_id = str(item.get("file_id") or item.get("id") or f"result-{index + 1:03d}")
    result_format = str(item.get("format") or _format_from_filename(filename))
    size_bytes = item.get("size_bytes") if item.get("size_bytes") is not None else item.get("size")
    out = dict(item)
    out.update(
        {
            "file_id": file_id,
            "filename": filename,
            "name": str(item.get("name") or filename),
            "format": result_format,
            "size_bytes": size_bytes,
            "size": size_bytes,
        }
    )
    return out


def _normalise_result_files(job: dict[str, Any]) -> None:
    result = job.get("result")
    if not isinstance(result, dict):
        return
    files = result.get("files")
    if not isinstance(files, list):
        result["files"] = []
        return
    result["files"] = [
        _normalise_result_file(item, index)
        for index, item in enumerate(files)
        if isinstance(item, dict)
    ]


def _format_from_filename(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith((".xml", ".xml.gz")):
        return "blast_xml"
    if lowered.endswith((".out", ".out.gz", ".tsv", ".txt")):
        return "blast_tabular"
    return "unknown"


def _normalise_external_job_payload(
    upstream: dict[str, Any],
    *,
    request_payload: dict[str, Any] | None = None,
    default_status: str = "queued",
) -> dict[str, Any]:
    payload = request_payload or {}
    out = dict(upstream)
    out["status"] = _public_status(out.get("status"), default=default_status)
    if out.get("submission_source") in (None, ""):
        out["submission_source"] = payload.get("submission_source") or "external_api"
    if out.get("external_correlation_id") in (None, ""):
        out["external_correlation_id"] = payload.get("external_correlation_id")
    if out.get("db_name") in (None, ""):
        out["db_name"] = out.get("db") or payload.get("db") or payload.get("database")
    if out.get("program") in (None, ""):
        out["program"] = payload.get("program")
    out.setdefault("blast_version", None)
    out.setdefault("db_version", None)
    _normalise_result_files(out)
    return out


def _openapi_scope_kwargs(
    *,
    subscription_id: str = "",
    resource_group: str = "",
    cluster_name: str = "",
) -> dict[str, str]:
    out: dict[str, str] = {}
    if subscription_id:
        out["subscription_id"] = subscription_id
    if resource_group:
        out["resource_group"] = resource_group
    if cluster_name:
        out["cluster_name"] = cluster_name
    return out


@router.post("/submit", status_code=202)
def submit_external_blast_job(
    request: ExternalBlastSubmitRequest,
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> dict[str, Any]:
    payload = request.model_dump(exclude_none=True)
    # Server-derived sharding default: a DB that exceeds a single node's RAM
    # (e.g. core_nt) MUST run sharded, which the sibling only does for a
    # sharding-family resource_profile. Promote a missing/standard profile so a
    # caller that omits it still gets a runnable job instead of a memory-fit
    # rejection. An explicit profile is preserved.
    payload["resource_profile"] = resolve_sharded_db_resource_profile(
        payload.get("db") or "", payload.get("resource_profile")
    )
    payload.update(
        canonical_submit_metadata(
            payload,
            submission_source="external_api",
            correlation_id=request.external_correlation_id,
        )
    )
    payload["canonical_request"] = canonical_submit_snapshot(payload)
    payload.update(submit_contracts(payload))
    from api.services.blast.provenance import build_blast_provenance

    payload["provenance"] = build_blast_provenance(
        job_id=str(payload["external_correlation_id"]),
        payload=payload,
    )
    LOGGER.info(
        "external BLAST submit accepted caller_oid=%s db=%s program=%s",
        redact_oid(caller.object_id),
        request.db,
        request.program,
    )
    del caller

    # Unified-ingress path (issue #36, default-OFF gate ENABLE_SB_SUBMIT_INGRESS):
    # instead of calling /v1/jobs directly, enqueue the request onto the Service
    # Bus queue so the dashboard's own consumer drains it (single consumer =
    # single writer). Returns immediately with the dashboard correlation id; the
    # OpenAPI job id is linked later by the consumer via the bridge record. A
    # publish failure falls back to the direct path below so a Service Bus blip
    # never drops a submit.
    correlation_id = str(payload["external_correlation_id"])
    from api.services.blast.submit_ingress import enqueue_submit_request, should_enqueue_submit

    if should_enqueue_submit():
        try:
            message_id = enqueue_submit_request(payload, correlation_id)
            LOGGER.info(
                "external BLAST submit enqueued to service bus corr=%s msg=%s",
                correlation_id,
                message_id,
            )
            from api.services.blast.external_query_labels import remember_inline_query_label

            # Key the remembered label by the correlation id; the consumer
            # re-remembers under the OpenAPI id once it knows it.
            remember_inline_query_label(correlation_id, request.query_fasta)
            try:
                from api.routes.blast.submit import _invalidate_message_flow_caches

                _invalidate_message_flow_caches()
            except Exception:  # pragma: no cover - best-effort display freshness
                LOGGER.debug("message-flow cache invalidate skipped after enqueue")
            return {
                "job_id": correlation_id,
                "status": "queued",
                "submission_source": "servicebus",
                "external_correlation_id": correlation_id,
                "ingress": "service_bus",
            }
        except Exception as exc:
            # Break-glass: a real publish failure falls back to the direct path
            # so the submit is never lost. Logged so the operator can see the
            # ingress degraded to direct.
            LOGGER.warning(
                "service bus enqueue failed corr=%s (%s); falling back to direct submit",
                correlation_id,
                type(exc).__name__,
            )

    # Pre-flight the sibling's submit path. Surfaces precise structured 503s
    # (e.g. AKS stopped / workload pool empty / openapi pod down) before we
    # spend the 90 s submit timeout waiting for a request the sibling cannot
    # service. Older sibling images without /v1/ready fail open inside the
    # client so the submit still goes through.
    scope_kwargs = _openapi_scope_kwargs(
        subscription_id=str(payload.get("subscription_id") or "").strip(),
        resource_group=str(payload.get("resource_group") or "").strip(),
        cluster_name=str(payload.get("cluster_name") or "").strip(),
    )
    external_blast.ready(**scope_kwargs)
    upstream = external_blast.submit_job(payload, **scope_kwargs)
    normalised = _normalise_external_job_payload(upstream, request_payload=payload)
    # The sibling OpenAPI plane stores no query identity for inline FASTA, so
    # remember a defline-derived label keyed by the upstream job id. The jobs
    # list enriches external rows with it instead of showing ``query.fa``.
    # Fully best-effort: must never 5xx an already-accepted submit.
    from api.services.blast.external_query_labels import remember_inline_query_label

    remember_inline_query_label(str(normalised.get("job_id") or ""), request.query_fasta)
    # Surface the new external job on the Message Flow card without waiting out
    # the external-jobs (~70 s) + monitor (~30 s) read caches. Lazy import keeps
    # this route free of an import-time dependency on the blast submit module.
    try:
        from api.routes.blast.submit import _invalidate_message_flow_caches

        _invalidate_message_flow_caches()
    except Exception:  # pragma: no cover - best-effort display freshness only
        LOGGER.debug("message-flow cache invalidate skipped after external submit")
    return normalised


@router.get("/jobs")
def list_external_blast_jobs(
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    cluster_name: str = Query(default=""),
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> dict[str, Any]:
    """Forward to the external ElasticBLAST OpenAPI `/v1/jobs` listing.

    The dashboard's own `/api/blast/jobs` only surfaces locally-recorded job
    rows (from JobStateRepository / Azure Table Storage). Jobs submitted
    directly through the sibling OpenAPI service live in the cluster's
    ConfigMaps and are invisible to that route. This proxy lets the BLAST
    Jobs page join both sources.

    The listing is served through the shared external-jobs cache (TTL +
    negative cache + in-flight de-duplication, same wrapper the combined
    `/api/blast/jobs` route uses). Without it, a stale or unreachable base
    URL costs the full ``_LIST_TIMEOUT_SECONDS`` then 503 on *every* poll,
    so this facade felt slower than the cached combined route (issue #30).
    """
    from api.services.blast.external_jobs import _external_list_jobs_cached

    LOGGER.info("external BLAST list requested caller_oid=%s", redact_oid(caller.object_id))
    del caller
    rows = _external_list_jobs_cached(
        _openapi_scope_kwargs(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        )
    )
    return {"jobs": rows, "count": len(rows)}


@router.get("/jobs/{job_id}")
def get_external_blast_job(
    job_id: str = Path(..., min_length=6, max_length=12, pattern=r"^[a-f0-9]+$"),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    cluster_name: str = Query(default=""),
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> dict[str, Any]:
    LOGGER.info(
        "external BLAST status requested caller_oid=%s job_id=%s",
        redact_oid(caller.object_id),
        job_id,
    )
    del caller
    scope_kwargs = _openapi_scope_kwargs(
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    return _normalise_external_job_payload(
        external_blast.get_job(job_id, **scope_kwargs),
        default_status="running",
    )


@router.get("/jobs/{job_id}/events")
def list_external_blast_job_events(
    job_id: str = Path(..., min_length=6, max_length=12, pattern=r"^[a-f0-9]+$"),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    cluster_name: str = Query(default=""),
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> dict[str, Any]:
    LOGGER.info(
        "external BLAST events requested caller_oid=%s job_id=%s",
        redact_oid(caller.object_id),
        job_id,
    )
    del caller
    scope_kwargs = _openapi_scope_kwargs(
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=cluster_name,
    )
    try:
        from api.services.blast.events import canonical_job_events
        from api.services.state_repo import get_state_repo

        rows = get_state_repo().get_history(job_id, limit=200)
        if rows:
            return {"job_id": job_id, "events": canonical_job_events(rows)}
    except Exception as exc:
        LOGGER.info("external BLAST local events unavailable: %s", type(exc).__name__)
    detail = external_blast.get_job(job_id, **scope_kwargs)
    status = str(detail.get("status") or detail.get("phase") or "unknown")
    return {
        "job_id": job_id,
        "events": [
            {
                "id": "current",
                "job_id": job_id,
                "event": status,
                "phase": status,
                "status": status,
                "timestamp": str(detail.get("updated_at") or detail.get("created_at") or ""),
                "payload": detail,
            }
        ],
    }


@router.get("/jobs/{job_id}/manifest")
def get_external_blast_job_manifest(
    job_id: str = Path(..., min_length=6, max_length=12, pattern=r"^[a-f0-9]+$"),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    cluster_name: str = Query(default=""),
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> dict[str, Any]:
    LOGGER.info(
        "external BLAST manifest requested caller_oid=%s job_id=%s",
        redact_oid(caller.object_id),
        job_id,
    )
    del caller
    from api.routes._blast_shared import _external_result_files
    from api.services.blast.result_manifest import build_result_manifest

    detail = external_blast.get_job(
        job_id,
        **_openapi_scope_kwargs(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        ),
    )
    files = _external_result_files(detail)
    return build_result_manifest(job_id=job_id, files=files, source="external")


@router.get("/jobs/{job_id}/files/{file_id}")
def download_external_blast_file(
    job_id: str = Path(..., min_length=6, max_length=12, pattern=r"^[a-f0-9]+$"),
    file_id: str = Path(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"),
    subscription_id: str = Query(default=""),
    resource_group: str = Query(default=""),
    cluster_name: str = Query(default=""),
    caller: CallerIdentity = _REQUIRE_CALLER,
) -> StreamingResponse:
    LOGGER.info(
        "external BLAST file requested caller_oid=%s job_id=%s file_id=%s",
        redact_oid(caller.object_id),
        job_id,
        file_id,
    )
    del caller
    downloaded = external_blast.stream_file(
        job_id,
        file_id,
        **_openapi_scope_kwargs(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        ),
    )
    return StreamingResponse(
        downloaded.chunks,
        media_type=downloaded.media_type,
        headers={"Content-Disposition": f'attachment; filename="{downloaded.filename}"'},
    )
