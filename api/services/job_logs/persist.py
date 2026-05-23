"""Persist k8s pod logs at BLAST job finalization.

Responsibility: Snapshot terminal-state ElasticBLAST pod logs into durable
storage so completed/failed jobs retain the real `blast` / `results-export` /
`init-ssd` container output, not just the `staging_db` azcopy stdout from
the submit task.
Edit boundaries: Pure service module — no FastAPI / Celery decorators.
Callers are the artifact finalizer task (`api/tasks/blast_artifacts.py`) and
its unit tests. Network I/O (Kubernetes API) and Storage I/O (chunked log
artifacts + job state row) are both side-effects of the public entry point.
Key entry points: `persist_completed_job_pod_logs`
Risky contracts: Must be idempotent (the finalizer can retry). Pod logs are
sanitised and truncated identically to the SSE follow path. Storage and
Table writes are best-effort; failures are logged and never raised to the
caller — losing tail logs must not break artifact finalization.
Validation: `uv run pytest -q api/tests/test_job_log_persist.py`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.job_logs.k8s import (
    K8sLogTarget,
    discover_k8s_log_targets,
    fetch_k8s_pod_log_tail,
    resolve_elastic_blast_job_id,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_TAIL_LINES = 200
LAST_OUTPUT_MAX_CHARS = 6_000
CHUNK_EVENT_COUNT = 100
LOG_NAMESPACE = "default"

# Containers whose tail should appear in the `last_output` summary that the
# UI renders under each step. Other containers (eg. shared sidecars) still
# go into chunked artifacts but stay out of the summary blob.
PHASE_PRIMARY_CONTAINERS: dict[str, tuple[str, ...]] = {
    "running": ("blast", "results-export"),
    "exporting_results": ("results-export",),
    "staging_db": ("get-blastdb", "import-query-batches"),
    "warming_up": ("vmtouch",),
}


def persist_completed_job_pod_logs(
    credential: TokenCredential,
    state: Any,
    *,
    tail_lines: int = DEFAULT_TAIL_LINES,
) -> dict[str, int]:
    """Fetch pod log tails for one terminal BLAST job and persist them.

    Returns a mapping of ``phase`` → ``number of events persisted``. Empty
    dict means nothing was persisted: no targets discovered, missing inputs,
    or a recoverable error (see logs).
    """

    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}
    subscription_id = str(
        getattr(state, "subscription_id", "") or payload.get("subscription_id") or ""
    )
    resource_group = str(
        getattr(state, "resource_group", "") or payload.get("resource_group") or ""
    )
    cluster_name = str(getattr(state, "cluster_name", "") or payload.get("cluster_name") or "")
    job_id = str(getattr(state, "job_id", "") or payload.get("job_id") or "")
    if not (subscription_id and resource_group and cluster_name and job_id):
        return {}

    elastic_job_id = resolve_elastic_blast_job_id(payload)
    try:
        targets = discover_k8s_log_targets(
            credential,
            subscription_id,
            resource_group,
            cluster_name,
            namespace=LOG_NAMESPACE,
            job_id=job_id,
            elastic_job_id=elastic_job_id,
        )
    except Exception as exc:
        LOGGER.info(
            "persist_completed_job_pod_logs: discovery skipped job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )
        return {}
    if not targets:
        return {}

    by_phase: dict[str, list[K8sLogTarget]] = defaultdict(list)
    for target in targets:
        by_phase[target.phase].append(target)

    all_events: dict[str, list[dict[str, Any]]] = {}
    primaries_text: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    for phase, group in by_phase.items():
        events: list[dict[str, Any]] = []
        primaries = set(PHASE_PRIMARY_CONTAINERS.get(phase, ()))
        index = 0
        for target in group:
            try:
                lines = fetch_k8s_pod_log_tail(
                    credential,
                    subscription_id,
                    resource_group,
                    cluster_name,
                    target,
                    tail_lines=tail_lines,
                )
            except Exception as exc:
                LOGGER.info(
                    "persist_completed_job_pod_logs: tail skipped %s/%s job_id=%s: %s",
                    target.pod_name,
                    target.container_name,
                    job_id,
                    type(exc).__name__,
                )
                continue
            if not lines:
                continue
            stream_name = f"{target.pod_name}/{target.container_name}"
            for line in lines:
                events.append({"stream": stream_name, "line": line, "index": index})
                index += 1
            if not primaries or target.container_name in primaries:
                primaries_text[phase].append((stream_name, lines))
        if events:
            all_events[phase] = events

    if not all_events:
        return {}

    try:
        from api.services.job_artifacts import write_execution_log_chunk
        from api.services.state.repository import JobStateRepository
    except Exception as exc:  # pragma: no cover - import-time failure
        LOGGER.warning(
            "persist_completed_job_pod_logs: import failed job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )
        return {}

    results: dict[str, int] = {}
    for phase, events in all_events.items():
        for chunk_seq, start in enumerate(range(0, len(events), CHUNK_EVENT_COUNT)):
            try:
                write_execution_log_chunk(
                    job_id,
                    phase,
                    chunk_seq,
                    events[start : start + CHUNK_EVENT_COUNT],
                )
            except Exception as exc:
                LOGGER.info(
                    "persist_completed_job_pod_logs: chunk skipped job_id=%s phase=%s seq=%s: %s",
                    job_id,
                    phase,
                    chunk_seq,
                    type(exc).__name__,
                )
                continue
        results[phase] = len(events)

    try:
        repo = JobStateRepository()
        fresh = repo.get(job_id) or state
        fresh_payload = (
            dict(fresh.payload) if isinstance(getattr(fresh, "payload", None), dict) else {}
        )
        progress = fresh_payload.get("_progress")
        if not isinstance(progress, dict):
            progress = {}
        steps = progress.get("steps")
        if not isinstance(steps, dict):
            steps = {}
        for phase, blocks in primaries_text.items():
            if not blocks:
                continue
            buf: list[str] = []
            for stream_name, lines in blocks:
                buf.append(f"--- {stream_name} ---")
                buf.extend(lines)
            joined = "\n".join(buf)
            if len(joined) > LAST_OUTPUT_MAX_CHARS:
                half = LAST_OUTPUT_MAX_CHARS // 2
                head = joined[:half]
                tail = joined[-half:]
                marker = "(truncated; full content in execution-steps log chunks)"
                joined = f"{head}\n...\n{marker}\n...\n{tail}"
            current_step = steps.get(phase) if isinstance(steps.get(phase), dict) else {}
            step = dict(current_step) if isinstance(current_step, dict) else {}
            current_last = str(step.get("last_output") or "")
            if len(joined) > len(current_last):
                step["last_output"] = joined
            step["pod_log_persisted"] = True
            steps[phase] = step
        progress["steps"] = steps
        fresh_payload["_progress"] = progress
        repo.update(job_id, payload=fresh_payload)
    except Exception as exc:
        LOGGER.info(
            "persist_completed_job_pod_logs: payload merge skipped job_id=%s: %s",
            job_id,
            type(exc).__name__,
        )

    return results


__all__ = ["persist_completed_job_pod_logs"]
