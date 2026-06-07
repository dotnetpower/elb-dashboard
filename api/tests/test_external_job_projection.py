"""Unit tests for the external (OpenAPI) job → dashboard projection.

Module summary: Drives `_external_to_blast_job` with raw OpenAPI job dicts to
pin the response contract the SPA's `BlastJobSummary` type depends on.

Responsibility: Verify the projected ``job_id`` is always a string (never None)
  so SPA navigation/keys never produce ``/blast/jobs/null``.
Edit boundaries: Pure projection — no Azure calls.
Key entry points: `test_external_job_id_is_always_a_string`.
Risky contracts: `BlastJobSummary.job_id` is a non-nullable string in
  `web/src/api/blast.types.ts`.
Validation: `uv run pytest -q api/tests/test_external_job_projection.py`.
"""

from __future__ import annotations

from api.services.blast.external_job_projection import _external_to_blast_job


def test_external_job_id_is_always_a_string_when_present() -> None:
    out = _external_to_blast_job({"job_id": "abc123", "status": "running"})
    assert out["job_id"] == "abc123"
    assert out["job_id_kind"] == "openapi"


def test_external_job_id_falls_back_to_empty_string_when_missing() -> None:
    """If the upstream OpenAPI response omits ``job_id``, the projection must
    emit an empty string, not ``None`` — the SPA's BlastJobSummary.job_id type
    is a non-nullable string and ``None`` would render ``/blast/jobs/null``."""
    out = _external_to_blast_job({"status": "queued"})
    assert out["job_id"] == ""
    assert isinstance(out["job_id"], str)
