"""AKS-fanout prepare-db Job parameter resolution.

Pure, env-driven resolution of the Kubernetes Job tuning knobs for the
AKS-fanout prepare-db path. Extracted from `api/routes/storage/prepare_db.py`
`_try_dispatch_aks_mode` (issue #24) so the route keeps HTTP validation /
response shaping and this layer owns the reusable, side-effect-free parameter
math that the route header's edit boundary says belongs in a service.

Responsibility: Parse the `PREPARE_DB_AKS_*` environment knobs into a validated
    `AksJobLimits` (parallelism, files-per-pod, image, deadline, and the
    optional azcopy-concurrency / backoff / TTL overrides).
Edit boundaries: Pure function — no Azure SDK, no IO, no HTTP. The route still
    owns dispatch, locking, metadata, and error mapping.
Key entry points: `AksJobLimits`, `resolve_aks_job_limits`,
    `prefer_server_side_for_small_db`.
Risky contracts: An unset / unparsable optional override stays `None` so the
    downstream `prepare_db_jobs` defaults apply — omitting an env var MUST keep
    the existing behaviour.
Validation: `uv run pytest -q api/tests/test_prepare_db_aks_params.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULT_IMAGE = "mcr.microsoft.com/azure-cli:2.81.0"
_DEFAULT_DEADLINE_SECONDS = 4 * 60 * 60

# Size below which an ``mode=auto`` prepare-db is cheaper on the server-side
# (`start_copy_from_url`) path than the AKS-fanout path. The AKS path pays a
# fixed bootstrap cost per Job (pod scheduling + image pull + `azcopy login`
# + the Celery poll granularity) that dwarfs the transfer for a small DB like
# `16S_ribosomal_RNA` (~18 MB / 15 files). Server-to-server async copy is
# near-instant at that scale and needs no cluster. Only consulted for
# ``mode=auto``; explicit ``mode=aks`` always honours the caller.
_DEFAULT_MIN_AKS_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GiB
# When NCBI does not report per-file sizes (every size is 0), fall back to a
# file-count gate so a large DB with unknown sizes is NOT misrouted to the
# slower server-side path. Few files + unknown size = treat as small.
_DEFAULT_MIN_AKS_FILE_COUNT = 30


def prefer_server_side_for_small_db(total_bytes: int, file_count: int) -> bool:
    """Return True when an ``mode=auto`` prepare-db should use the server-side
    path instead of AKS-fanout because the database is small enough that the
    AKS Job bootstrap overhead dominates the transfer.

    Decision (env-overridable):
      * sizes known (``total_bytes > 0``): server-side iff
        ``total_bytes < PREPARE_DB_AKS_MIN_TOTAL_BYTES``.
      * sizes unknown (``total_bytes == 0``): server-side iff
        ``file_count <= PREPARE_DB_AKS_MIN_FILE_COUNT`` — never strand a
        large, unknown-size DB on the slow path on a count it cannot beat.

    Pure / side-effect-free so the route stays thin and this is unit-testable.
    """
    min_bytes = _int_or(
        _DEFAULT_MIN_AKS_TOTAL_BYTES,
        os.environ.get("PREPARE_DB_AKS_MIN_TOTAL_BYTES", str(_DEFAULT_MIN_AKS_TOTAL_BYTES)),
        minimum=0,
    )
    min_count = _int_or(
        _DEFAULT_MIN_AKS_FILE_COUNT,
        os.environ.get("PREPARE_DB_AKS_MIN_FILE_COUNT", str(_DEFAULT_MIN_AKS_FILE_COUNT)),
        minimum=0,
    )
    if total_bytes > 0:
        return total_bytes < min_bytes
    return file_count <= min_count


@dataclass(frozen=True)
class AksJobLimits:
    """Resolved Kubernetes Job tuning knobs for the AKS-fanout prepare-db path.

    The three `*_or_none` fields are `None` when their env var is unset or
    unparsable, so the `prepare_db_jobs` builder applies its own defaults.
    """

    max_pods: int
    files_per_pod: int
    image: str
    active_deadline_seconds: int
    azcopy_concurrency: int | None
    backoff_limit: int | None
    ttl_seconds_after_finished: int | None

    def task_overrides(self) -> dict[str, int]:
        """Only the optional overrides that are actually set.

        Spread into the Celery task kwargs so an unset override never pins a
        value, preserving the builder's default behaviour.
        """
        out: dict[str, int] = {}
        if self.azcopy_concurrency is not None:
            out["azcopy_concurrency"] = self.azcopy_concurrency
        if self.backoff_limit is not None:
            out["backoff_limit"] = self.backoff_limit
        if self.ttl_seconds_after_finished is not None:
            out["ttl_seconds_after_finished"] = self.ttl_seconds_after_finished
        return out


def _int_or(default: int, raw: str, *, minimum: int, maximum: int | None = None) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _optional_int(raw: str, *, minimum: int, maximum: int | None = None) -> int | None:
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def resolve_aks_job_limits() -> AksJobLimits:
    """Resolve the `PREPARE_DB_AKS_*` env knobs into validated Job limits."""
    return AksJobLimits(
        max_pods=_int_or(10, os.environ.get("PREPARE_DB_AKS_MAX_PARALLELISM", "10"), minimum=1),
        files_per_pod=_int_or(
            50, os.environ.get("PREPARE_DB_AKS_FILES_PER_POD", "50"), minimum=1
        ),
        image=os.environ.get("PREPARE_DB_AKS_AZCOPY_IMAGE", "").strip() or _DEFAULT_IMAGE,
        active_deadline_seconds=_int_or(
            _DEFAULT_DEADLINE_SECONDS,
            os.environ.get("PREPARE_DB_AKS_JOB_TIMEOUT_SECONDS", str(_DEFAULT_DEADLINE_SECONDS)),
            minimum=60,
        ),
        azcopy_concurrency=_optional_int(
            os.environ.get("PREPARE_DB_AKS_AZCOPY_CONCURRENCY", ""), minimum=1, maximum=512
        ),
        backoff_limit=_optional_int(
            os.environ.get("PREPARE_DB_AKS_BACKOFF_LIMIT", ""), minimum=0
        ),
        ttl_seconds_after_finished=_optional_int(
            os.environ.get("PREPARE_DB_AKS_TTL_SECONDS", ""), minimum=60
        ),
    )
