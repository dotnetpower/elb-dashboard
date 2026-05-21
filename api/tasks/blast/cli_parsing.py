"""ElasticBLAST CLI argv builder, stdout parser, and retry classifier.

Responsibility: Build elastic-blast argv, parse its stdout JSON envelopes, and classify
retry-vs-fail decisions for submit/cancel results.
Edit boundaries: Pure functions — no Azure SDK, no Celery, no I/O. Symbols are re-exported
from ``api.tasks.blast`` so test monkeypatches on ``blast._X`` keep working.
Key entry points: ``_elastic_blast_argv``, ``_last_json``, ``_result_error``,
``_is_retryable_result``, ``_retry_after``, ``_submit_success_status``,
``_extract_elastic_blast_job_id``.
Risky contracts: ``RETRYABLE_ERROR_CATEGORIES`` / ``RETRYABLE_EXIT_CODES`` drive retry
behaviour for every submit task; widening either changes retry semantics globally.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from api.tasks import blast as _blast

ELASTIC_BLAST_CFG_FILE = "elastic-blast.ini"
ELASTIC_BLAST_JOB_ID_RE = re.compile(r"/results/[^/]+/(job-[A-Za-z0-9_-]+)")
RETRYABLE_ERROR_CATEGORIES = {"transient", "capacity", "conflict"}
RETRYABLE_EXIT_CODES = {8, 10}


def _elastic_blast_argv(
    command: str,
    job_id: str,
    *,
    cfg_file: str = ELASTIC_BLAST_CFG_FILE,
    force: bool = False,
) -> list[str]:
    del job_id, force
    return [
        "elastic-blast",
        command,
        "--cfg",
        cfg_file,
    ]


def _last_json(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _result_error(result: Mapping[str, Any], payload: Mapping[str, Any] | None) -> str:
    if payload and payload.get("kind") == "error":
        return _blast._snippet(payload.get("message"))
    return _blast._snippet(
        result.get("stderr") or result.get("stdout") or "elastic-blast failed"
    )


def _is_retryable_result(
    result: Mapping[str, Any], payload: Mapping[str, Any] | None
) -> bool:
    category = str((payload or {}).get("category", ""))
    if category in RETRYABLE_ERROR_CATEGORIES:
        return True
    try:
        return int(result.get("exit_code", 1)) in RETRYABLE_EXIT_CODES
    except (TypeError, ValueError):
        return False


def _retry_after(payload: Mapping[str, Any] | None, default: int) -> int:
    raw = (payload or {}).get("retry_after_seconds")
    try:
        parsed = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, 300))


def _submit_success_status(payload: Mapping[str, Any] | None) -> tuple[str, str]:
    decision = str((payload or {}).get("decision", "accepted"))
    details = (payload or {}).get("details")
    terminal = details.get("terminal") if isinstance(details, dict) else None
    if decision == "already_done" and terminal == "SUCCESS":
        return "completed", "completed"
    if decision == "already_done" and terminal == "FAILURE":
        return "failed", "failed"
    return "submitted", "running"


def _extract_elastic_blast_job_id(output: object) -> str:
    text = str(output or "")
    match = ELASTIC_BLAST_JOB_ID_RE.search(text)
    return match.group(1) if match else ""


__all__ = (
    "ELASTIC_BLAST_CFG_FILE",
    "ELASTIC_BLAST_JOB_ID_RE",
    "RETRYABLE_ERROR_CATEGORIES",
    "RETRYABLE_EXIT_CODES",
    "_elastic_blast_argv",
    "_extract_elastic_blast_job_id",
    "_is_retryable_result",
    "_last_json",
    "_result_error",
    "_retry_after",
    "_submit_success_status",
)
