"""Auto-retry eligibility + bookkeeping for transient-failed BLAST jobs.

Responsibility: Decide whether a terminal-failed BLAST job may be auto-resubmitted,
reconstruct the original ``submit`` task kwargs from the persisted job row, and
compute the next ``auto_retry`` bookkeeping block (attempt counter, backoff,
quarantine). All side effects (state writes, re-enqueue) stay in the task layer.
Edit boundaries: Pure decision logic plus reads off a ``JobState``; no Storage
writes, no Celery enqueue, no Azure calls here. The feature gate
``BLAST_AUTO_RETRY_ENABLED`` defaults OFF (charter section 12a Rule 4).
Key entry points: ``auto_retry_enabled``, ``evaluate``, ``restore_submit_kwargs``,
``read_meta``.
Risky contracts: Only ``FailureCategory.TRANSIENT_INFRA`` jobs are ever returned
as ``retry``. ``restore_submit_kwargs`` returns ``None`` when a required submit
field is missing — the caller MUST quarantine rather than enqueue a malformed
submit. The attempt counter lives in ``payload["auto_retry"]`` (no dedicated
column); the sweep is bounded by ``BLAST_AUTO_RETRY_MAX`` and a per-sweep cap.
Validation: ``uv run pytest -q api/tests/test_blast_auto_retry.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from api.services.blast.failure_classification import classify_failure

_DEFAULT_MAX_RETRIES = 2
_DEFAULT_SWEEP_LIMIT = 5
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 1800

# Required kwargs for a faithful resubmit. Missing any => not restorable.
_REQUIRED_FIELDS = (
    "subscription_id",
    "resource_group",
    "cluster_name",
    "storage_account",
    "program",
    "database",
    "query_file",
)


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def auto_retry_enabled() -> bool:
    """Master gate. Default OFF — the sweep is a no-op until explicitly enabled."""
    return _env_bool("BLAST_AUTO_RETRY_ENABLED")


def max_auto_retries() -> int:
    return _env_int("BLAST_AUTO_RETRY_MAX", _DEFAULT_MAX_RETRIES, minimum=1, maximum=10)


def sweep_limit() -> int:
    return _env_int("BLAST_AUTO_RETRY_SWEEP_LIMIT", _DEFAULT_SWEEP_LIMIT, minimum=1, maximum=50)


def max_scan() -> int:
    """Upper bound on failed rows read per sweep (bounds Table read cost)."""
    return _env_int("BLAST_AUTO_RETRY_SCAN_LIMIT", 200, minimum=10, maximum=1000)


def backoff_seconds(attempt: int) -> int:
    """Exponential backoff for the *next* attempt (attempt index is 0-based)."""
    exp = max(0, attempt)
    return min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2**exp))


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class AutoRetryMeta:
    count: int = 0
    max: int = 0
    last_attempt_at: str = ""
    quarantined: bool = False
    last_error_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "max": self.max,
            "last_attempt_at": self.last_attempt_at,
            "quarantined": self.quarantined,
            "last_error_code": self.last_error_code,
        }


def read_meta(payload: Any) -> AutoRetryMeta:
    """Read the ``auto_retry`` bookkeeping block off a job payload (best-effort)."""
    block = payload.get("auto_retry") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        return AutoRetryMeta(max=max_auto_retries())
    try:
        count = int(block.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    return AutoRetryMeta(
        count=max(0, count),
        max=max_auto_retries(),
        last_attempt_at=str(block.get("last_attempt_at") or ""),
        quarantined=bool(block.get("quarantined")),
        last_error_code=str(block.get("last_error_code") or ""),
    )


def restore_submit_kwargs(state: Any) -> dict[str, Any] | None:
    """Rebuild the original ``submit`` task kwargs from a persisted job row.

    Prefers the normalised ``JobState`` columns (immutable, canonicalised at
    submit) and falls back to the stored request ``payload`` for the two fields
    that have no column (``query_file``, ``options``). Returns ``None`` when any
    required field is missing so the caller quarantines instead of enqueuing a
    malformed submit.
    """
    payload = state.payload if isinstance(getattr(state, "payload", None), dict) else {}

    def col_or_payload(column: str, *payload_keys: str) -> str:
        val = str(getattr(state, column, "") or "")
        if val:
            return val
        for key in payload_keys:
            pv = payload.get(key)
            if pv:
                return str(pv)
        return ""

    kwargs: dict[str, Any] = {
        "job_id": str(getattr(state, "job_id", "") or ""),
        "subscription_id": col_or_payload("subscription_id", "subscription_id"),
        "resource_group": col_or_payload("resource_group", "resource_group"),
        "cluster_name": col_or_payload("cluster_name", "cluster_name"),
        "storage_account": col_or_payload("storage_account", "storage_account"),
        "program": col_or_payload("program", "program"),
        "database": col_or_payload("db", "database", "db"),
        "query_file": str(payload.get("query_file") or ""),
        "caller_oid": str(getattr(state, "owner_oid", "") or ""),
        "caller_tenant_id": str(getattr(state, "tenant_id", "") or ""),
    }
    options = payload.get("options")
    kwargs["options"] = options if isinstance(options, dict) else None

    if not kwargs["job_id"]:
        return None
    for required in _REQUIRED_FIELDS:
        if not kwargs.get(required):
            return None
    return kwargs


RetryAction = Literal["retry", "quarantine", "skip"]


@dataclass(frozen=True)
class RetryDecision:
    action: RetryAction
    reason: str
    kwargs: dict[str, Any] | None = None
    next_meta: AutoRetryMeta | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def evaluate(state: Any, *, now: datetime | None = None) -> RetryDecision:
    """Decide what the sweep should do with one job row.

    Returns ``skip`` for anything not a transient-infra terminal failure, a job
    still inside its backoff window, or one already quarantined. Returns
    ``quarantine`` when the retry budget is exhausted or the submit kwargs cannot
    be restored. Returns ``retry`` (with restored kwargs + the next meta block)
    only when a resubmit is safe and due.
    """
    now = now or _now()

    if str(getattr(state, "status", "") or "") != "failed":
        return RetryDecision("skip", "not_failed")
    if getattr(state, "parent_job_id", None):
        return RetryDecision("skip", "split_child")

    # Never resubmit a job that originated outside the dashboard (Service Bus
    # drain / external OpenAPI plane). Those jobs are owned by the producing
    # system; the control plane has no mandate to re-run them and doing so would
    # duplicate work the sibling may already be retrying.
    if str(getattr(state, "submission_source", "") or "") in {"external_api", "servicebus"}:
        return RetryDecision("skip", "external_origin")
    _payload = getattr(state, "payload", None)
    if isinstance(_payload, dict) and isinstance(_payload.get("external"), dict):
        return RetryDecision("skip", "external_origin")

    error_code = str(getattr(state, "error_code", "") or "")
    classification = classify_failure(error_code, str(getattr(state, "phase", "") or ""))
    if not classification.auto_retryable:
        return RetryDecision("skip", f"not_auto_retryable:{classification.category.value}")

    meta = read_meta(getattr(state, "payload", None))
    if meta.quarantined:
        return RetryDecision("skip", "already_quarantined")

    if meta.count >= meta.max:
        quarantined = AutoRetryMeta(
            count=meta.count,
            max=meta.max,
            last_attempt_at=meta.last_attempt_at,
            quarantined=True,
            last_error_code=error_code,
        )
        return RetryDecision(
            "quarantine",
            "retry_budget_exhausted",
            next_meta=quarantined,
        )

    # Backoff window: measured from the last attempt, or the row's failure time
    # (updated_at) for the first retry.
    anchor = _parse_iso(meta.last_attempt_at) or _parse_iso(
        str(getattr(state, "updated_at", "") or "")
    )
    if anchor is not None:
        due_at = anchor.timestamp() + backoff_seconds(meta.count)
        if now.timestamp() < due_at:
            return RetryDecision("skip", "backoff_not_elapsed")

    kwargs = restore_submit_kwargs(state)
    if kwargs is None:
        quarantined = AutoRetryMeta(
            count=meta.count,
            max=meta.max,
            last_attempt_at=meta.last_attempt_at,
            quarantined=True,
            last_error_code=error_code,
        )
        return RetryDecision(
            "quarantine",
            "submit_kwargs_unrestorable",
            next_meta=quarantined,
        )

    next_meta = AutoRetryMeta(
        count=meta.count + 1,
        max=meta.max,
        last_attempt_at=now.isoformat(timespec="seconds"),
        quarantined=False,
        last_error_code=error_code,
    )
    return RetryDecision("retry", "due", kwargs=kwargs, next_meta=next_meta)


def merge_meta_into_payload(payload: Any, meta: AutoRetryMeta) -> dict[str, Any]:
    """Return a new payload dict with the ``auto_retry`` block set to ``meta``."""
    base = dict(payload) if isinstance(payload, dict) else {}
    base["auto_retry"] = meta.as_dict()
    return base
