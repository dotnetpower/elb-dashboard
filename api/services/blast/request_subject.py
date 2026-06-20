"""Compose a human-meaningful Service Bus Subject for a BLAST request message.

Every BLAST request message historically carried the constant Subject
``blast.request``, so an operator looking at the request queue (Azure portal /
Service Bus Explorer, or the dashboard Playground / Message Flow peek) could not
tell one queued job from another. This module derives a natural, distinguishable
Subject from the request body — program, database, and the first query defline —
while preserving the ``blast.request`` fallback when nothing meaningful exists.

Responsibility: One pure derivation — ``build_request_subject(body)`` — turning a
    request body (the OpenAPI ``/v1/jobs`` submit shape the producers already
    build) into a short Subject string. NO Service Bus access, NO Redis, NO
    side effects.
Edit boundaries: Keep this pure and dependency-free apart from reusing
    ``derive_inline_query_label``. No FastAPI, no Celery, no Azure SDK.
Key entry points: ``build_request_subject``, ``DEFAULT_REQUEST_SUBJECT``.
Risky contracts: The Subject is display/identification only — the consumer never
    routes or filters on it (the drain path preserves whatever Subject it sees
    and falls back to ``blast.request`` on requeue), so changing it cannot break
    drain. Derivation must NEVER raise: any failure returns
    ``DEFAULT_REQUEST_SUBJECT`` so a malformed body can never fail a send.
Validation: ``uv run pytest -q api/tests/test_request_subject.py``.
"""

from __future__ import annotations

from typing import Any

from api.services.blast.external_query_labels import derive_inline_query_label

# Preserved fallback Subject — keeps the historical machine-recognisable value
# for messages that carry no program/db/query identity (and is what the drain
# requeue path already falls back to).
DEFAULT_REQUEST_SUBJECT = "blast.request"

# Service Bus Subject is free-form, but a long query defline must not bloat the
# message envelope or the peek preview. Cap defensively.
_MAX_SUBJECT_CHARS = 120

# Visual separator between the program/db head and the query label. Matches the
# middot the SPA already uses elsewhere (e.g. the version stamp).
_SEPARATOR = " \u00b7 "


def build_request_subject(body: dict[str, Any] | None) -> str:
    """Return a natural, distinguishable Subject for a BLAST request message.

    Composition (segments joined by a middot):

    * ``"{program} {db}"`` — e.g. ``"blastn core_nt"`` (either part omitted when
      empty);
    * the first query defline derived from inline FASTA, e.g. ``"sp|P12345"`` or
      ``"sp|P12345 (+2)"`` for a multi-record FASTA.

    Examples::

        blastn core_nt · sp|P12345 (+2)
        blastp nr
        blast.request            # nothing meaningful in the body

    Never raises — any derivation failure returns ``DEFAULT_REQUEST_SUBJECT`` so
    a malformed body can never turn an otherwise-valid send into an error.
    """
    try:
        data = body if isinstance(body, dict) else {}
        program = str(data.get("program") or "").strip()
        db = str(data.get("db") or "").strip()
        query_fasta = data.get("query_fasta")
        label = (
            derive_inline_query_label(query_fasta)
            if isinstance(query_fasta, str) and query_fasta
            else ""
        )
    except Exception:
        return DEFAULT_REQUEST_SUBJECT

    head = " ".join(part for part in (program, db) if part)
    segments = [segment for segment in (head, label) if segment]
    if not segments:
        return DEFAULT_REQUEST_SUBJECT

    subject = _SEPARATOR.join(segments)[:_MAX_SUBJECT_CHARS].strip()
    return subject or DEFAULT_REQUEST_SUBJECT
