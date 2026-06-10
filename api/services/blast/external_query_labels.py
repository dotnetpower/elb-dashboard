"""Bridge the inline-FASTA query identity from external BLAST submit to the jobs list.

Responsibility: Derive a human-meaningful query label from an inline FASTA at
    external (OpenAPI) submit time and stash it in OPS Redis so the jobs-list
    projection can show it. The sibling OpenAPI execution plane uploads inline
    FASTA to ``queries/<job_id>.fa`` and stores NO query identity (no filename,
    no defline) on the job record, so without this bridge every API-submitted
    job renders as the generic ``query.fa`` placeholder in Recent searches.
Edit boundaries: Pure derivation + best-effort Redis get/set only. No FastAPI,
    no Celery, no Azure SDK. Redis access goes through
    ``api.services.redis_clients.get_ops_redis_client`` (never
    ``redis.Redis.from_url``). Every Redis call is best-effort and swallows
    failures — a Redis outage must never break submit or list.
Key entry points: ``derive_inline_query_label``, ``remember_query_label``,
    ``recall_query_label``, ``apply_remembered_query_label``.
Risky contracts: This only ENRICHES a display label; it must never decide which
    rows appear or mutate scope/owner. ``apply_remembered_query_label`` returns
    a row unchanged when it already carries a query identity, so a real
    sibling-provided ``query_file`` always wins over the remembered label.
Validation: ``uv run pytest -q api/tests/test_external_query_labels.py``.
"""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

# OPS Redis key namespace + TTL. The label only needs to survive from submit
# until the first jobs-list call materialises the external job into the
# jobstate Table (which then carries the label permanently). A 7-day TTL gives
# the user a generous bridge window and self-evicts afterwards.
_KEY_PREFIX = "elb:blast:extquery:"
_TTL_SECONDS = 7 * 24 * 3600
# Cap a single label so a pathological FASTA header cannot bloat the Table row
# or the jobs-list payload. ``canonical_job_metadata`` caps query_label to 240;
# stay well under that.
_MAX_LABEL_CHARS = 120


def derive_inline_query_label(query_fasta: str) -> str:
    """Return a short query label derived from inline FASTA text.

    Uses the first record's sequence id (the token after ``>``) and, when the
    FASTA carries more than one record, appends ``(+N)`` so a multi-query
    submit is distinguishable. Returns ``""`` when no FASTA header is present
    (the caller then leaves the existing generic fallback in place).
    """
    if not isinstance(query_fasta, str) or not query_fasta:
        return ""
    first_id = ""
    count = 0
    for raw_line in query_fasta.splitlines():
        line = raw_line.strip()
        if not line.startswith(">"):
            continue
        count += 1
        if not first_id:
            header = line[1:].strip()
            first_id = header.split(None, 1)[0] if header else ""
    if not first_id:
        return ""
    first_id = first_id[:_MAX_LABEL_CHARS]
    if count > 1:
        return f"{first_id} (+{count - 1})"
    return first_id


def remember_query_label(job_id: str, label: str) -> None:
    """Best-effort: persist ``label`` for ``job_id`` in OPS Redis with a TTL."""
    if not job_id or not label:
        return
    try:
        from api.services.redis_clients import get_ops_redis_client

        client = get_ops_redis_client()
        client.set(_KEY_PREFIX + job_id, label, ex=_TTL_SECONDS)
    except Exception as exc:  # pragma: no cover - best-effort, Redis optional
        LOGGER.debug("remember_query_label skipped job_id=%s: %s", job_id, type(exc).__name__)


def remember_inline_query_label(job_id: str, query_fasta: str) -> None:
    """Best-effort: derive a label from ``query_fasta`` and remember it for ``job_id``.

    This is the single entry point the submit routes call. Both the derivation
    and the Redis write are wrapped so a successful BLAST submit is NEVER turned
    into a 5xx by this display-only side effect — re-raising here would make the
    client retry a job that was already accepted by the OpenAPI plane.
    """
    try:
        label = derive_inline_query_label(query_fasta)
    except Exception as exc:  # pragma: no cover - derive is pure + defensive
        LOGGER.debug(
            "derive_inline_query_label skipped job_id=%s: %s", job_id, type(exc).__name__
        )
        return
    remember_query_label(job_id, label)



def recall_query_label(job_id: str) -> str:
    """Best-effort: return the remembered label for ``job_id`` (``""`` if none)."""
    if not job_id:
        return ""
    try:
        from api.services.redis_clients import get_ops_redis_client

        client = get_ops_redis_client()
        value = client.get(_KEY_PREFIX + job_id)
    except Exception as exc:  # pragma: no cover - best-effort, Redis optional
        LOGGER.debug("recall_query_label skipped job_id=%s: %s", job_id, type(exc).__name__)
        return ""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return str(value)


def apply_remembered_query_label(ext_row: dict[str, Any]) -> dict[str, Any]:
    """Inject a remembered query label into an external job row when it has none.

    The jobs-list projection reads ``query_file`` / ``query`` to build the
    query label. A real sibling-provided value always wins: when the row
    already carries one this is a no-op. Otherwise the remembered label (if
    any) is injected as ``query_file`` so the projection and the frontend
    ``externalQueryLabel`` both surface it. Returns a shallow copy only when a
    label is injected so the caller's row is never mutated unexpectedly.
    """
    if not isinstance(ext_row, dict):
        return ext_row
    if ext_row.get("query_file") or ext_row.get("query"):
        return ext_row
    label = recall_query_label(str(ext_row.get("job_id") or ""))
    if not label:
        return ext_row
    enriched = dict(ext_row)
    enriched["query_file"] = label
    return enriched
