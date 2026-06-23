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
``results_prefix_from_state``, ``elastic_blast_subdir_prefix``.
Risky contracts: The returned prefix ALWAYS ends with a single ``/`` and never
contains ``..`` — a bare ``{job_id}`` (no trailing slash) was a latent
prefix-collision bug (``name_starts_with="job-abc"`` also matches
``job-abcd/...``); normalizing here fixes it. External (``/v1/jobs``) jobs keep
the flat ``{job_id}/`` layout per the sibling's contract, so the default is the
correct value for them and #67 must not change it.
Validation: ``uv run pytest -q api/tests/test_storage_job_prefix.py``.
"""

from __future__ import annotations

from typing import Any


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


def elastic_blast_subdir_prefix(results_prefix: str) -> str:
    """Prefix for discovering the elastic-blast ``job-<id>`` subdirectory.

    elastic-blast writes its run under ``<results_prefix>job-<id>/...``; the
    discovery scan needs ``<results_prefix>job-``. ``results_prefix`` must
    already be normalized (trailing slash).
    """
    return f"{results_prefix}job-"
