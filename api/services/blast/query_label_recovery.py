"""Durable recovery of an external BLAST job's Query ID for the detail view.

Responsibility: Decide whether a job detail response still shows the generic
    ``query.fa`` placeholder instead of a real query identity and, when it does,
    recover the defline from the durable query blob, persist it onto the
    jobstate row, and bound every retry path so the POLLED detail route cannot
    turn a one-shot backfill into per-poll Storage/Table traffic.
Edit boundaries: Orchestration only — the defline derivation lives in
    ``api.services.blast.external_query_labels``, the blob read in
    ``api.services.blast.job_state.derive_external_query_label``, and the row
    write in the state repository. No FastAPI request/response handling here;
    the route passes in the already-loaded ``JobState`` and repository.
Key entry points: ``recover_detail_query_label``.
Risky contracts: This is DISPLAY-ONLY. It must never change which rows a caller
    can see, must never raise anything other than the ownership ``HTTPException``
    raised by the underlying reader, and must preserve ``updated_at`` on the row
    (the SPA renders "Runtime · Workflow" as ``created_at -> updated_at``, so a
    backfill that defaults the timestamp to *now* silently inflates a finished
    job's elapsed time). Every branch must terminate: a hit ends the loop by
    writing a real label onto the row, and every non-hit writes the short-TTL
    negative marker.
Validation: ``uv run pytest -q api/tests/test_blast_jobs_routes.py
    api/tests/test_external_query_labels.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from api.auth import CallerIdentity
from api.services.blast.external_query_labels import (
    is_generic_query_label,
    query_label_miss_recorded,
    remember_query_label,
    remember_query_label_miss,
)
from api.services.blast.job_state import (
    derive_external_query_label,
    external_payload_of,
)

LOGGER = logging.getLogger(__name__)


def recover_detail_query_label(
    state: Any,
    repo: Any,
    caller: CallerIdentity,
    *,
    current_label: Any,
) -> str:
    """Return a recovered Query ID for ``state``, or ``""`` to keep the current one.

    External (OpenAPI / Service Bus) jobs remember their inline-FASTA defline
    only in ephemeral OPS Redis, which is wiped on every Container App revision
    restart. After that the projection stamps the generic ``query.fa`` filename
    placeholder, so the detail header contradicts the prepare-step FASTA preview
    right below it — the preview reads the real blob and shows the real defline.
    This reads that same blob once, persists the defline onto the row, and
    returns it.

    The detail route is POLLED, so the guard order is load-bearing:

    * ``external_payload_of`` is a pure dict lookup on the row already in hand.
      It runs FIRST because ``query.fa`` is also the dashboard's own upload
      basename — without it, every poll of every dashboard job would pay a Redis
      round trip for a recovery that can never apply.
    * A hit writes a real label onto the row, so the placeholder guard is False
      on every subsequent poll.
    * Every non-hit (empty defline, unreadable blob, a defline that is itself the
      placeholder, or a failed row write) records the short-TTL negative marker,
      bounding the Storage read to once per TTL instead of once per poll.

    When OPS Redis is unavailable the markers degrade to no-ops and the recovery
    simply re-runs — the same cost profile the route had before they existed.
    """
    if external_payload_of(state) is None:
        return ""
    if not is_generic_query_label(current_label):
        return ""
    job_id = str(getattr(state, "job_id", "") or "")
    if not job_id or query_label_miss_recorded(job_id):
        return ""

    try:
        recovered = derive_external_query_label(state, caller)
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.debug(
            "query label recovery skipped job_id=%s: %s", job_id, type(exc).__name__
        )
        recovered = ""

    if is_generic_query_label(recovered):
        # Covers "" AND a defline that is literally ">query.fa". Accepting the
        # latter would re-enter this branch on the NEXT poll (the guard tests the
        # same predicate), turning a one-shot backfill into a per-poll Storage
        # read + Table write + history row for as long as the tab stays open.
        remember_query_label_miss(job_id)
        return ""

    try:
        repo.update(
            job_id,
            query_label=recovered,
            # Echoed back explicitly: the repository defaults ``updated_at`` to
            # *now*, and a display-only backfill must not inflate a finished
            # job's rendered elapsed time.
            updated_at=str(getattr(state, "updated_at", "") or "") or None,
        )
        # One-shot per job and it durably mutates a row, so log it: without this
        # the query_label silently changes value between two list responses with
        # nothing to grep for.
        LOGGER.info("query label recovered from query blob job_id=%s", job_id)
        remember_query_label(job_id, recovered)
    except Exception as exc:
        # The row did not take the label, so the guard would be True again on the
        # next poll. Fall back to the negative marker: this response still shows
        # the recovered label, and the retry is bounded to once per marker TTL.
        remember_query_label_miss(job_id)
        LOGGER.debug(
            "query label persist skipped job_id=%s: %s", job_id, type(exc).__name__
        )
    return recovered
