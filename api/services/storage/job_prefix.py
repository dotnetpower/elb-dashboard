"""Canonical per-job Storage prefix resolution (system-of-record).

Responsibility: Single source of truth for the results/query blob prefix of a
BLAST job. Replaces the ad-hoc ``f"{job_id}/"`` reconstruction scattered across
result-listing call sites so the physical layout is decoupled from ``job_id``
and a future date-tiered layout (issue #67) only changes what gets *written*
into ``JobState.results_prefix`` — every reader keeps resolving through here.
Edit boundaries: Pure string normalization plus reading an already-fetched
``JobState`` field. No Azure SDK clients, no Table reads, no network — callers
that have a state row pass it; callers that only have a ``job_id`` get the
legacy ``{job_id}/`` fallback.
Key entry points: ``normalize_results_prefix``, ``default_results_prefix``,
``build_dated_results_prefix``, ``results_prefix_from_state``,
``resolve_results_prefix``, ``date_layout_enabled``, ``elastic_blast_subdir_prefix``.
Risky contracts: The returned prefix ALWAYS ends with a single ``/`` and never
contains ``..`` — a bare ``{job_id}`` (no trailing slash) was a latent
prefix-collision bug (``name_starts_with="job-abc"`` also matches
``job-abcd/...``); normalizing here fixes it. External (``/v1/jobs``) jobs keep
the flat ``{job_id}/`` layout per the sibling's contract, so the default is the
correct value for them and #67 must not change it.
Validation: ``uv run pytest -q api/tests/test_storage_job_prefix.py``.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger(__name__)

_DATE_LAYOUT_ENV = "STORAGE_DATE_LAYOUT_ENABLED"
_ON_VALUES = {"1", "true", "yes", "on"}


def date_layout_enabled() -> bool:
    """Return True when new submissions write results under a date-tiered prefix.

    Default OFF (charter §12a Rule 4). When OFF, ``results_job_url`` and the
    submit route keep the legacy flat ``{job_id}/`` layout and the read-side
    resolver skips its Table lookup (every job is flat, so ``{job_id}/`` is
    always correct). Flipping it ON only affects *new* submissions; existing
    flat jobs keep resolving via their stored ``results_prefix`` (= ``{job_id}/``)
    so the two layouts coexist without a migration.

    LIMITATION (do not flip ON until resolved): queries/uploads and the
    ``queries`` config blob stay flat by design (separate container; queries are
    deleted per-job so they need no date-bucketing — see #74). Split jobs ARE
    date-aware as of #75 (parent merge output + readiness probes + path-key
    builders all resolve through ``resolve_results_prefix``; children stay flat
    and self-consistent). Flipping the flag ON still requires the blob
    soft-delete safety net (#76) and a live-cluster validation pass.
    """
    return os.environ.get(_DATE_LAYOUT_ENV, "").strip().lower() in _ON_VALUES


def normalize_results_prefix(prefix: str | None, job_id: str) -> str:
    """Return a collision-free, single-trailing-slash results prefix.

    An empty / whitespace / slash-only ``prefix`` falls back to the job's own
    id. ``..`` segments are stripped defensively (the security validation in
    issue #70 is the authoritative guard, but a resolver must never emit a
    traversal-bearing prefix).
    """
    raw = (prefix or "").strip().strip("/")
    if not raw or any(part == ".." for part in raw.split("/")):
        raw = (job_id or "").strip().strip("/")
    return f"{raw}/"


def default_results_prefix(job_id: str) -> str:
    """Legacy / fallback flat layout: ``{job_id}/``.

    Used by call sites that only have a ``job_id`` (no state row in hand). In
    issue #66 this equals every stored ``results_prefix`` value, so routing the
    inline reconstructions through here is a behaviour-preserving refactor.
    """
    return normalize_results_prefix("", job_id)


def build_dated_results_prefix(job_id: str, *, now: datetime | None = None) -> str:
    """Date-tiered results prefix: ``YYYY/MM/DD/{job_id}/`` (UTC).

    Computed once at submit time and persisted to ``JobState.results_prefix``
    so every later read/write derives from the stored value rather than
    recomputing the date (which would drift across a midnight boundary). Nesting
    is fixed at three date segments + the job id; do not nest deeper (each path
    segment is an ACL-traversal cost on an HNS account).
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y/%m/%d")
    return normalize_results_prefix(f"{stamp}/{job_id}", job_id)


def dated_results_subdir(*, now: datetime | None = None) -> str:
    """Return ONLY the date directory ``YYYY/MM/DD/`` (UTC), no job id.

    Used for the EXTERNAL (sibling OpenAPI ``/v1/jobs``) submit path: the
    dashboard forwards this as ``results_prefix`` and the sibling appends its
    OWN job id, writing results under ``results/<YYYY/MM/DD>/<openapi_job_id>/``.
    This mirrors the native date tiering (:func:`build_dated_results_prefix`,
    which appends the dashboard job id) so both submit surfaces land under the
    same ``YYYY/MM/DD/`` shape. Always ends with a single trailing slash.
    """
    return (now or datetime.now(UTC)).strftime("%Y/%m/%d/")


def results_prefix_from_state(state: Any) -> str:
    """Authoritative results prefix for a job from its ``JobState`` row.

    Reads the durable ``results_prefix`` column (populated by ``to_entity`` at
    create time) and normalizes it, falling back to ``{job_id}/`` for legacy
    rows persisted before the column existed. Callers that already hold the
    state row should prefer this over :func:`default_results_prefix` so the
    stored (and, post-#67, possibly date-tiered) value is honoured.
    """
    job_id = str(getattr(state, "job_id", "") or "")
    stored = getattr(state, "results_prefix", None)
    return normalize_results_prefix(stored if isinstance(stored, str) else "", job_id)


def resolve_results_prefix(
    job_id: str, *, state: Any | None = None, repo: Any | None = None
) -> str:
    """Resolve a job's results prefix, honouring the stored (dated) value.

    Resolution order:
      1. an explicit ``state`` row's ``results_prefix`` (no I/O), else
      2. when :func:`date_layout_enabled`, a single ``jobstate`` lookup by
         ``job_id`` (the row carries the canonical, possibly date-tiered prefix),
         else
      3. the legacy ``{job_id}/`` fallback.

    Step 2 is gated on the flag so the OFF path stays zero-I/O: with date layout
    disabled every job is flat and ``{job_id}/`` is always correct, so no Table
    read is paid. With it ON, old flat jobs resolve to their stored ``{job_id}/``
    and new dated jobs to ``YYYY/MM/DD/{job_id}/`` — the two coexist with no
    migration. A lookup failure degrades to the flat fallback (never raises).
    """
    if state is not None:
        stored = getattr(state, "results_prefix", None)
        if isinstance(stored, str) and stored.strip():
            return normalize_results_prefix(stored, job_id)
    if date_layout_enabled():
        try:
            if repo is None:
                from api.services.state_repo import get_state_repo

                repo = get_state_repo()
            row = repo.get(job_id)
            stored = getattr(row, "results_prefix", None) if row is not None else None
            if isinstance(stored, str) and stored.strip():
                return normalize_results_prefix(stored, job_id)
        except Exception as exc:
            # Degrade to the flat fallback — a resolver must never raise into a
            # listing/streaming path.
            LOGGER.debug(
                "results prefix lookup degraded to flat job_id=%s: %s",
                job_id,
                type(exc).__name__,
            )
    return default_results_prefix(job_id)



def elastic_blast_subdir_prefix(results_prefix: str) -> str:
    """Prefix for discovering the elastic-blast ``job-<id>`` subdirectory.

    elastic-blast writes its run under ``<results_prefix>job-<id>/...``; the
    discovery scan needs ``<results_prefix>job-``. ``results_prefix`` must
    already be normalized (trailing slash).
    """
    return f"{results_prefix}job-"
