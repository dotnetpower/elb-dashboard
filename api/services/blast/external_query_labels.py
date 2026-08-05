"""Bridge the inline-FASTA query identity from external BLAST submit to the jobs list.

Responsibility: Derive a human-meaningful query label from an inline FASTA at
    external (OpenAPI) submit time and stash it in OPS Redis so the jobs-list
    projection can show it. The sibling OpenAPI execution plane uploads inline
    FASTA to ``queries/<job_id>.fa`` and stores NO query identity (no filename,
    no defline) on the job record, so without this bridge every API-submitted
    job renders as the generic ``query.fa`` placeholder in Recent searches.
Edit boundaries: Pure derivation + best-effort Redis get/set only. No FastAPI,
    no Celery, no Azure SDK (``api.services.sanitise`` is a pure regex helper and
    is allowed). Redis access goes through
    ``api.services.redis_clients.get_ops_redis_client`` (never
    ``redis.Redis.from_url``). Every Redis call is best-effort and swallows
    failures — a Redis outage must never break submit or list.
Key entry points: ``derive_inline_query_label``, ``remember_query_label``,
    ``recall_query_label``, ``apply_remembered_query_label``,
    ``is_generic_query_label``, ``remember_query_label_miss``,
    ``query_label_miss_recorded``.
Risky contracts: This only ENRICHES a display label; it must never decide which
    rows appear or mutate scope/owner. ``apply_remembered_query_label`` returns
    a row unchanged when it already carries a query identity, so a real
    sibling-provided ``query_file`` always wins over the remembered label.
    The defline is ATTACKER-CONTROLLED (any API caller picks it, and the
    Storage-backed recovery re-reads it from the query blob), so
    ``derive_inline_query_label`` MUST keep masking secrets and stripping
    control characters before the value reaches the Table row or the SPA.
Validation: ``uv run pytest -q api/tests/test_external_query_labels.py``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from api.services.sanitise import sanitise

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
# Display placeholders the projection substitutes when NO query identity is
# known (``external_job_projection`` stamps ``"query.fa"``; the run-detail FASTA
# preview labels the blob ``input.fa``). They are filenames, not deflines, so
# every surface asking "does this row already carry a query identity?" must
# treat them as absent — otherwise a placeholder that got persisted onto the
# Table row permanently blocks the durable Storage-backed recovery, and the
# header keeps claiming ``query.fa`` while the FASTA preview right below it
# shows the real defline.
GENERIC_QUERY_LABELS = frozenset({"query.fa", "input.fa"})


def is_generic_query_label(value: Any) -> bool:
    """Return True when ``value`` is empty or one of the generic placeholders."""
    text = str(value or "").strip().lower()
    return not text or text in GENERIC_QUERY_LABELS


# Negative marker for the durable Storage-backed label recovery. The job detail
# route is POLLED, so an external job whose query blob is missing, unreadable,
# or header-less would otherwise re-pay a Storage read on EVERY poll forever —
# the positive cache can never fill for it. A separate key namespace keeps
# ``recall_query_label`` (used on the LIST path) returning "" for a miss instead
# of leaking a sentinel value into the UI. The TTL is short so a transient
# Storage/RBAC blip recovers within minutes; a query blob is immutable, so a
# slightly stale negative is harmless.
_MISS_KEY_PREFIX = "elb:blast:extquerymiss:"
_MISS_TTL_SECONDS = 600


def remember_query_label_miss(job_id: str) -> None:
    """Best-effort: mark that label recovery ran for ``job_id`` and found nothing."""
    if not job_id:
        return
    try:
        from api.services.redis_clients import get_ops_redis_client

        get_ops_redis_client().set(_MISS_KEY_PREFIX + job_id, "1", ex=_MISS_TTL_SECONDS)
    except Exception as exc:  # pragma: no cover - best-effort, Redis optional
        LOGGER.debug(
            "remember_query_label_miss skipped job_id=%s: %s", job_id, type(exc).__name__
        )


def query_label_miss_recorded(job_id: str) -> bool:
    """Best-effort: True when a recent recovery attempt for ``job_id`` found nothing.

    Degrades to False when Redis is unavailable — the recovery then re-runs, which
    is the same cost profile the route had before the marker existed.
    """
    if not job_id:
        return False
    try:
        from api.services.redis_clients import get_ops_redis_client

        return get_ops_redis_client().get(_MISS_KEY_PREFIX + job_id) is not None
    except Exception as exc:  # pragma: no cover - best-effort, Redis optional
        LOGGER.debug(
            "query_label_miss_recorded skipped job_id=%s: %s", job_id, type(exc).__name__
        )
        return False


# Control characters (including NUL) are stripped from a derived label. An
# Azure Table property value cannot carry them, so an entity write with a NUL
# in the defline would throw; they are also invisible noise in the SPA header.
# ANSI CSI sequences are already removed by ``sanitise``.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
# Upper bound on the raw token fed to ``sanitise``. Long enough that a full SAS
# query string still matches its regex (so it is masked as a whole rather than
# truncated into a partially-visible signature), short enough that a
# pathological whitespace-free defline cannot turn the regex pipeline into a
# CPU sink. The blob-recovery reader caps its read at 512 bytes anyway.
_SANITISE_INPUT_CAP = 512


def _clean_label_token(token: str) -> str:
    """Mask secrets and strip control characters from a raw defline token.

    The defline is attacker-controlled: an API caller can submit
    ``>https://acct.blob.core.windows.net/c/b?sv=…&sig=…`` and, without this,
    the SAS would be persisted onto the job row and rendered in the Recent
    searches list and the Run details header (charter §12: UI output is
    sanitised). Applied at the single derivation point so BOTH the submit-time
    bridge and the Storage-backed recovery are covered.
    """
    cleaned = _CONTROL_CHARS_RE.sub("", sanitise(token[:_SANITISE_INPUT_CAP]))
    return cleaned.strip()[:_MAX_LABEL_CHARS]


def derive_inline_query_label(query_fasta: str) -> str:
    """Return a short query label derived from inline FASTA text.

    Uses the first record's sequence id (the token after ``>``) and, when the
    FASTA carries more than one record, appends ``(+N)`` so a multi-query
    submit is distinguishable. Returns ``""`` when no FASTA header is present
    (the caller then leaves the existing generic fallback in place) or when the
    header holds nothing but secrets / control characters.

    The token is masked by :func:`_clean_label_token` before it is returned —
    the defline is attacker-controlled and this value is persisted and rendered.
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
    first_id = _clean_label_token(first_id)
    if not first_id:
        return ""
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
