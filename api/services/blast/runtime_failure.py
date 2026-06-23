"""Cluster-side BLAST runtime-failure detail reader (shared low-level helper).

When a BLAST job fails the dashboard otherwise only sees a coarse
``status=failed`` signal — either the dashboard's own K8s summary (for jobs
submitted through the Celery pipeline) or the sibling OpenAPI service's generic
``error`` string (for ``/v1/jobs`` submissions). The authoritative blastn
diagnostics live in the workload results container, written by the
elastic-blast runner. This module reads them so both projection paths can
surface the real failure cause instead of a generic placeholder.

Responsibility: Best-effort read of ``metadata/FAILURE.txt`` (captured blastn
    stderr) and ``logs/BLAST_RUNTIME-NNN.out`` (the ``run exitCode`` line) from
    the workload results container, returning one concise sanitisable line.
Edit boundaries: Pure read-only Storage access via the shared MI credential;
    NO writes, NO ARM calls, NEVER raises (degrades to ``""``). The caller owns
    sanitisation / clamping before the string is surfaced to the UI.
Key entry points: ``read_blast_runtime_failure``.
Risky contracts: Returns ``""`` on any error (missing account/job id,
    credential failure, blob list/read failure) so callers can fall back to a
    generic message. The captured ``FAILURE.txt`` stderr is redacted via
    ``sanitise`` at the source (so the dashboard K8s-refresh step error AND the
    external-job projection both surface secret-free text); callers should still
    clamp length before persisting to a Table row.
Validation: ``uv run pytest -q api/tests/test_local_to_blast_job.py
    api/tests/test_external_blast_api.py``.
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)


def read_blast_runtime_failure(storage_account: str, job_id: str) -> str:
    """Best-effort cluster-side blastn failure detail for a failed job.

    On a K8s search failure the dashboard otherwise only sees the generic
    ``status=failed`` pod/Job summary (or, for external OpenAPI jobs, a generic
    ``one or more BLAST jobs failed`` string). The elastic-blast runner writes
    the real diagnostics into the results container:

    * ``.../logs/BLAST_RUNTIME-NNN.out`` ends with ``run exitCode NNN <code>``.
    * ``.../metadata/FAILURE.txt`` carries the captured blastn stderr (best-
      effort upload by the runner; frequently absent).

    ``job_id`` is the results-container prefix — the dashboard job id for
    Celery-submitted jobs, or the sibling OpenAPI job id for ``/v1/jobs``
    submissions (the runner stores results under ``results/<job_id>/...`` in
    both cases). Returns a concise one-line message, or ``""`` when nothing is
    readable so the caller can fall back to a generic message.
    """
    if not storage_account or not job_id:
        return ""
    try:
        from api.services import get_credential
        from api.services.storage.blob_io import list_result_blobs, read_blob_text
        from api.services.storage.job_prefix import default_results_prefix

        credential = get_credential()
        blobs = list_result_blobs(
            credential, storage_account, "results", default_results_prefix(job_id), max_results=2000
        )
    except Exception as exc:
        LOGGER.debug(
            "blast failure-detail listing skipped job_id=%s: %s", job_id, type(exc).__name__
        )
        return ""

    failure_path = ""
    runtime_path = ""
    for blob in blobs:
        name = str(blob.get("name") or "")
        if name.endswith("/metadata/FAILURE.txt"):
            failure_path = name
        elif "/logs/BLAST_RUNTIME-" in name and name.endswith(".out") and not runtime_path:
            runtime_path = name

    exit_code = ""
    if runtime_path:
        try:
            text = read_blob_text(
                credential, storage_account, "results", runtime_path, max_bytes=4096
            )
            for line in text.splitlines():
                if "run exitCode" in line:
                    exit_code = line.split()[-1]
        except Exception as exc:
            LOGGER.debug("blast runtime read skipped job_id=%s: %s", job_id, type(exc).__name__)

    stderr_text = ""
    if failure_path:
        try:
            raw_stderr = read_blob_text(
                credential, storage_account, "results", failure_path, max_bytes=4096
            )
            # FAILURE.txt is runner-captured stderr (blastn + best-effort azcopy
            # diagnostics) and can embed a SAS query string / Bearer token /
            # subscription GUID. Redact at the source so EVERY caller — the
            # dashboard K8s-refresh step error AND the external-job projection —
            # surfaces a sanitised message (Charter §12: UI-shown output is
            # sanitised). ANSI control sequences are stripped by `sanitise` too.
            from api.services.sanitise import sanitise

            stderr_text = sanitise(raw_stderr).strip()
        except Exception as exc:
            LOGGER.debug("blast FAILURE.txt read skipped job_id=%s: %s", job_id, type(exc).__name__)

    if stderr_text:
        head = stderr_text.replace("\n", " ").strip()[:500]
        if exit_code and exit_code not in {"0", ""}:
            return f"BLAST search exited with code {exit_code}: {head}"
        return f"BLAST search failed on the cluster: {head}"
    if exit_code and exit_code not in {"0", ""}:
        return (
            f"BLAST search exited with code {exit_code} on the cluster "
            "(no stderr was captured by the runner). A common cause is the "
            "database not being staged on the assigned node — re-run the DB "
            "warmup for this database and resubmit."
        )
    return ""
