"""Runtime classification helpers for DB order-oracle fan-out.

Responsibility: Classify expected Kubernetes Job summaries, validate exact
    run-scoped oracle part blobs, and best-effort clean up terminal Jobs.
Edit boundaries: Pure classification plus bounded per-run Storage/Kubernetes
    inspection; build claims, readiness planning, task orchestration, and HTTP
    shaping belong to their owning modules.
Key entry points: `classify_oracle_jobs`, `validate_oracle_parts`,
    `cleanup_oracle_jobs`, `OracleJobProgress`.
Risky contracts: A retrying Job with a nonzero failed pod count is not terminal
    until Kubernetes reports `status=Failed`; ready publication requires exact
    expected `.txt` names and every blob size greater than zero.
Validation: `uv run pytest -q api/tests/test_oracle_runtime.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class OracleJobProgress:
    status: str
    complete: tuple[str, ...]
    failed: tuple[str, ...]
    running: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def signature(self) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        return (self.status, self.complete, self.failed, self.missing)


def classify_oracle_jobs(
    expected_names: list[str], jobs: list[dict[str, Any]]
) -> OracleJobProgress:
    expected = tuple(sorted(set(expected_names)))
    by_name = {
        str(job.get("name") or ""): job for job in jobs if str(job.get("name") or "") in expected
    }
    complete: list[str] = []
    failed: list[str] = []
    running: list[str] = []
    missing: list[str] = []
    for name in expected:
        job = by_name.get(name)
        if job is None:
            missing.append(name)
            continue
        status = str(job.get("status") or "")
        if status == "Complete":
            complete.append(name)
        elif status == "Failed":
            failed.append(name)
        else:
            running.append(name)
    derived = "failed" if failed else "complete" if len(complete) == len(expected) else "running"
    return OracleJobProgress(
        status=derived,
        complete=tuple(complete),
        failed=tuple(failed),
        running=tuple(running),
        missing=tuple(missing),
    )


def validate_oracle_parts(
    container: Any,
    *,
    expected_paths: list[str],
    part_prefix: str,
) -> dict[str, Any]:
    expected = set(expected_paths)
    actual_sizes = {
        str(blob.name): int(getattr(blob, "size", 0) or 0)
        for blob in container.list_blobs(name_starts_with=part_prefix)
        if str(getattr(blob, "name", "")).endswith(".txt")
    }
    actual = set(actual_sizes)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    empty = sorted(name for name in expected & actual if actual_sizes[name] <= 0)
    return {
        "ready": not missing and not unexpected and not empty,
        "ready_parts": len(expected & actual) - len(empty),
        "expected_parts": len(expected),
        "missing": missing,
        "unexpected": unexpected,
        "empty": empty,
    }


def cleanup_oracle_jobs(
    credential: Any,
    *,
    subscription_id: str,
    cluster_resource_group: str,
    cluster_name: str,
    namespace: str,
    job_names: list[str],
) -> dict[str, Any]:
    from api.services.k8s.monitoring import k8s_job_delete

    deleted: list[str] = []
    errors: list[dict[str, str]] = []
    for job_name in job_names:
        try:
            result = k8s_job_delete(
                credential,
                subscription_id,
                cluster_resource_group,
                cluster_name,
                namespace,
                job_name,
            )
            if result.get("deleted") is False and result.get("error"):
                errors.append({"name": job_name, "error": str(result["error"])[:200]})
            else:
                deleted.append(job_name)
        except Exception as exc:
            errors.append({"name": job_name, "error": type(exc).__name__})
    return {"deleted": deleted, "errors": errors}


__all__ = [
    "OracleJobProgress",
    "classify_oracle_jobs",
    "cleanup_oracle_jobs",
    "validate_oracle_parts",
]
