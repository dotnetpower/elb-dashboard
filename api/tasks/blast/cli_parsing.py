"""ElasticBLAST CLI argv builder, stdout parser, and retry classifier.

Responsibility: Build elastic-blast argv, parse its stdout JSON envelopes, and classify
retry-vs-fail decisions for submit/cancel results.
Edit boundaries: Pure functions — no Azure SDK, no Celery, no I/O. Symbols are re-exported
from ``api.tasks.blast`` so test monkeypatches on ``blast._X`` keep working.
Key entry points: ``_elastic_blast_argv``, ``_elastic_blast_loglevel``, ``_last_json``,
``_result_error``, ``_is_retryable_result``, ``_retry_after``, ``_submit_success_status``,
``_extract_elastic_blast_job_id``.
Risky contracts: ``RETRYABLE_ERROR_CATEGORIES`` / ``RETRYABLE_EXIT_CODES`` drive retry
behaviour for every submit task; widening either changes retry semantics globally.
``_elastic_blast_argv`` pins ``--logfile stderr`` so the dashboard's live stream captures
ElasticBLAST's full progress log; dropping it reverts to upstream's file-only logging where
all INFO progress detail is hidden from the UI.
Validation: ``uv run pytest -q api/tests/test_blast_tasks.py``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any

from api.tasks import blast as _blast

ELASTIC_BLAST_CFG_FILE = "elastic-blast.ini"
ELASTIC_BLAST_JOB_ID_RE = re.compile(r"/results/[^/]+/(job-[A-Za-z0-9_-]+)")
RETRYABLE_ERROR_CATEGORIES = {"transient", "capacity", "conflict"}
RETRYABLE_EXIT_CODES = {8, 10}
ELASTIC_BLAST_VALID_LOGLEVELS = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
DEFAULT_ELASTIC_BLAST_LOGLEVEL = "INFO"


def _elastic_blast_loglevel() -> str:
    """Resolve the ElasticBLAST CLI log level (env-overridable).

    Upstream defaults to ``DEBUG`` which is noisy; the dashboard streams ``INFO``
    by default — enough to surface every submit progress marker (uploads, batch
    counts, "Submitting N jobs to cluster") without the low-level SDK chatter.
    Set ``ELASTIC_BLAST_LOGLEVEL=DEBUG`` to crank it up when diagnosing a submit.
    """
    raw = os.environ.get(
        "ELASTIC_BLAST_LOGLEVEL", DEFAULT_ELASTIC_BLAST_LOGLEVEL
    ).strip().upper()
    return raw if raw in ELASTIC_BLAST_VALID_LOGLEVELS else DEFAULT_ELASTIC_BLAST_LOGLEVEL


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
        # Route ElasticBLAST's full log stream to stderr so the dashboard's live
        # terminal stream captures it line-by-line. Upstream defaults to
        # ``--logfile elastic-blast.log`` with a WARNING-only stderr handler,
        # which buries every INFO progress line (query splitting, workfile
        # upload, "Submitting N jobs to cluster") in a file inside the ephemeral
        # terminal sidecar that the UI never reads — leaving the submit step
        # looking like it only emits a handful of yellow ``print()`` markers.
        "--logfile",
        "stderr",
        "--loglevel",
        _elastic_blast_loglevel(),
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
    text = str(
        result.get("stderr") or result.get("stdout") or "elastic-blast failed"
    )
    # ElasticBLAST now routes its full INFO log to stderr (see
    # ``_elastic_blast_argv``), so the actionable error / traceback is at the END
    # of the stream. Take the tail rather than the head so the INFO preamble does
    # not crowd out the real failure message.
    return _tail_snippet(text, 500)


# ElasticBLAST's submit pre-flight rejects a full-database run whose memory
# requirement exceeds the selected machine type's RAM, e.g.::
#
#   ERROR: BLAST database .../core_nt memory requirements exceed memory
#   available on selected machine type "Standard_E16s_v5". Please select
#   machine type with at least 251.7GB available memory.
#
# The message is accurate but not actionable for a dashboard user, who cannot
# edit the generated INI directly. Detect it so the failure surfaces the two
# supported remediations instead of an opaque exit-code-1 error.
_INSUFFICIENT_MEMORY_RE = re.compile(
    r"memory requirements exceed memory available", re.IGNORECASE
)

# ElasticBLAST raises the same opaque INPUT_ERROR (exit code 1) when the
# configured per-search ``mem-limit`` is larger than the selected machine
# type's usable RAM, e.g.::
#
#   Memory limit "200G" exceeds memory available on the selected machine type
#   Standard_E16s_v5: 124GB. Please, select machine type with more memory or
#   lower memory limit
#
# The dashboard's advanced options expose ``mem_limit``, so this is reachable
# from the UI and belongs to the same machine-type/resource mismatch class as
# the database-memory rejection above. Unlike the full-DB case, sharding does
# not help here — the fix is a smaller limit or a larger node.
_MEMORY_LIMIT_RE = re.compile(
    r"memory limit .*exceeds memory available", re.IGNORECASE
)

INSUFFICIENT_MEMORY_GUIDANCE = (
    "This database does not fit in the cluster node's memory for a "
    'full-database BLAST. Select the "Sharded throughput" execution profile '
    "to partition the database across nodes so each shard fits node memory, "
    "or recreate the cluster with a larger machine type, then resubmit."
)

MEMORY_LIMIT_GUIDANCE = (
    "The configured per-search memory limit is larger than the selected "
    "cluster node can provide. Lower the BLAST memory limit in advanced "
    "options, or recreate the cluster with a larger machine type, then "
    "resubmit."
)


def _submit_failure_guidance(error_text: object) -> str | None:
    """Return an actionable remediation hint for a known submit failure, or ``None``.

    Recognises ElasticBLAST's pre-flight resource rejections — both of which
    surface as the same opaque exit-code-1 ``INPUT_ERROR`` the dashboard cannot
    edit the generated INI to avoid:

    * full-DB BLAST that does not fit the cluster's node SKU -> steer to the
      "Sharded throughput" execution profile or a larger-SKU cluster;
    * per-search ``mem-limit`` larger than the node's usable RAM -> steer to a
      lower limit or a larger-SKU cluster.

    The database-memory check is evaluated first because its message is more
    specific; the memory-limit message starts with "Memory limit" so the two
    never collide. Pure function — safe to unit-test in isolation.
    """
    text = str(error_text or "")
    if not text:
        return None
    if _INSUFFICIENT_MEMORY_RE.search(text):
        return INSUFFICIENT_MEMORY_GUIDANCE
    if _MEMORY_LIMIT_RE.search(text):
        return MEMORY_LIMIT_GUIDANCE
    return None


def _tail_snippet(value: object, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return "..." + text[-(limit - 3):]


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
    "INSUFFICIENT_MEMORY_GUIDANCE",
    "MEMORY_LIMIT_GUIDANCE",
    "RETRYABLE_ERROR_CATEGORIES",
    "RETRYABLE_EXIT_CODES",
    "_elastic_blast_argv",
    "_extract_elastic_blast_job_id",
    "_is_retryable_result",
    "_last_json",
    "_result_error",
    "_retry_after",
    "_submit_failure_guidance",
    "_submit_success_status",
)
