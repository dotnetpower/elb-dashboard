"""Critical preflight gates evaluated synchronously before BLAST submit enqueue.

Responsibility: Translate the implicit "things that must be true before the BLAST
submit Celery task is enqueued" set into a structured fail-closed report. Surface
each gate with a stable ``id`` + ``error_code`` so the SPA can map directly to a
remediation action and the submit route can reject the request *before* writing a
queued job row that would otherwise sit in `queued` waiting for a task that can
never succeed (terminal sidecar down, exec token unset, AKS stopped, DB missing,
broker offline).
Edit boundaries: Pure service module. No FastAPI here — the route in
``api.routes.blast.submit`` calls ``evaluate_submit_gates`` and shapes the HTTP
response. Each gate is monkeypatchable individually via its module-level function
name; tests that need a different outcome should patch ``evaluate_submit_gates``
on this module so the route's local ``from … import …`` picks up the stub.
Key entry points: ``GateResult``, ``SubmitGatesReport``, ``evaluate_submit_gates``,
``reset_submit_gates_cache``.
Risky contracts: Results are cached per-process with a 5s TTL keyed by the
(subscription, RG, cluster) and (storage_account, database) tuples. The cache
amortises rapid retries from a single user but is intentionally short-lived so a
real fix (start cluster, prepare DB) shows up on the next submit. Local sidecar
gates (terminal_sidecar / exec_token / broker) are never cached — they are cheap
and must reflect the current process state.
Validation: ``uv run pytest -q api/tests/test_blast_submit_gates.py``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, replace
from time import monotonic
from typing import Any, Literal

LOGGER = logging.getLogger(__name__)

GateStatus = Literal["ok", "fail", "unknown"]
GateSeverity = Literal["critical", "warning"]

# Per-process cache TTL for the gates that hit ARM / Storage. Five seconds is
# long enough to absorb a UI retry burst from a single click but short enough
# that "user just started the cluster" reflects on the next attempt.
_CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class GateResult:
    """One named precondition with its outcome and a remediation hint."""

    id: str
    status: GateStatus
    severity: GateSeverity
    error_code: str
    message: str
    action: str | None = None
    action_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SubmitGatesReport:
    """Aggregate of all gate evaluations for a single submit attempt."""

    ok: bool
    gates: list[GateResult]
    blocking: list[GateResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "gates": [g.to_dict() for g in self.gates],
            "blocking": [g.to_dict() for g in self.blocking],
        }


_cache: dict[str, tuple[float, GateResult]] = {}


def _cache_get(key: str) -> GateResult | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if monotonic() - ts > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: GateResult) -> None:
    _cache[key] = (monotonic(), value)


def reset_submit_gates_cache() -> None:
    """Clear the per-process gate cache. Tests call this between cases."""
    _cache.clear()


def _gate_exec_token() -> GateResult:
    """``EXEC_TOKEN`` is the authorization the api/worker presents to the
    terminal sidecar. Missing means every shell call is rejected with 401, so
    the entire submit pipeline is dead before the first ``az`` invocation."""
    if os.environ.get("EXEC_TOKEN", "").strip():
        return GateResult(
            id="exec_token",
            status="ok",
            severity="critical",
            error_code="",
            message="EXEC_TOKEN is configured.",
        )
    return GateResult(
        id="exec_token",
        status="fail",
        severity="critical",
        error_code="exec_token_missing",
        message=(
            "EXEC_TOKEN env var is empty; the api/worker cannot call the terminal "
            "sidecar. Configure the exec-token secret."
        ),
        action="Configure exec-token secret",
        action_type="configure_exec_token",
    )


def _gate_terminal_sidecar() -> GateResult:
    """Probe the terminal sidecar's loopback ``/healthz``. Any failure here
    means ``az`` / ``kubectl`` / ``elastic-blast`` calls from the submit task
    will fail synchronously inside the worker."""
    try:
        from api.services import terminal_exec

        terminal_exec.healthz()
    except Exception as exc:
        LOGGER.warning("submit gate: terminal sidecar probe failed: %s", type(exc).__name__)
        return GateResult(
            id="terminal_sidecar",
            status="fail",
            severity="critical",
            error_code="terminal_sidecar_unavailable",
            message=(
                f"Terminal sidecar is not reachable ({type(exc).__name__}). "
                "Restart the terminal sidecar before submitting."
            ),
            action="Restart terminal sidecar",
            action_type="restart_terminal_sidecar",
        )
    return GateResult(
        id="terminal_sidecar",
        status="ok",
        severity="critical",
        error_code="",
        message="Terminal sidecar is reachable.",
    )


def _gate_broker() -> GateResult:
    """Cheap Redis ping. ``_safe_delay`` would otherwise raise 503 after the
    job row has been persisted; checking up front means we can reject before
    writing anything to the state repo."""
    try:
        from api.celery_app import celery_app

        conn = celery_app.connection()
        conn.ensure_connection(max_retries=1, timeout=2)
        conn.close()
    except Exception as exc:
        LOGGER.warning("submit gate: broker probe failed: %s", type(exc).__name__)
        return GateResult(
            id="broker",
            status="fail",
            severity="critical",
            error_code="broker_unavailable",
            message=(
                f"Task broker (Redis) is not reachable ({type(exc).__name__}). "
                "Verify the redis sidecar is healthy."
            ),
            action="Verify redis sidecar",
            action_type="restart_broker",
        )
    return GateResult(
        id="broker",
        status="ok",
        severity="critical",
        error_code="",
        message="Task broker is reachable.",
    )


def _gate_aks_cluster(
    *, subscription_id: str, resource_group: str, cluster_name: str
) -> GateResult:
    """Verify the target AKS cluster exists in the given RG and is Running.
    Cached per (subscription, RG, cluster) for 5s to absorb retry bursts.
    Unverifiable (ARM throttling / RBAC / private endpoint) is reported as
    ``status=unknown`` so the caller can decide whether to override."""
    cache_key = f"aks:{subscription_id}:{resource_group}:{cluster_name}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        from api.services import get_credential
        from api.services.monitoring import list_aks_clusters

        clusters = list_aks_clusters(get_credential(), subscription_id, resource_group)
        match = next((c for c in clusters if c.get("name") == cluster_name), None)
        if match is None:
            result = GateResult(
                id="aks_cluster",
                status="fail",
                severity="critical",
                error_code="cluster_not_found",
                message=(
                    f"AKS cluster '{cluster_name}' not found in '{resource_group}'."
                ),
                action="Select an existing cluster",
                action_type="select_cluster",
            )
        elif match.get("power_state") != "Running":
            power = match.get("power_state") or "unknown"
            result = GateResult(
                id="aks_cluster",
                status="fail",
                severity="critical",
                error_code="cluster_not_ready",
                message=f"AKS cluster '{cluster_name}' is {power}. Start it first.",
                action="Start cluster",
                action_type="start_cluster",
            )
        else:
            result = GateResult(
                id="aks_cluster",
                status="ok",
                severity="critical",
                error_code="",
                message=f"AKS cluster '{cluster_name}' is running.",
            )
    except Exception as exc:
        LOGGER.warning("submit gate: AKS probe failed: %s", type(exc).__name__)
        result = GateResult(
            id="aks_cluster",
            status="unknown",
            severity="critical",
            error_code="cluster_check_unavailable",
            message=f"Could not verify AKS cluster ({type(exc).__name__}).",
        )
    _cache_set(cache_key, result)
    return result


def _gate_blast_database(*, storage_account: str, database: str) -> GateResult:
    """Confirm the selected BLAST database has at least one ``.nsq/.psq/.nal/.pal``
    marker blob under ``blast-db/<prefix>``. Cached per (storage_account,
    database) for 5s. Storage RBAC / network failures land as ``unknown``."""
    cache_key = f"db:{storage_account}:{database}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        from api.services.blast.task_config import (
            BlastDatabaseAvailabilityError,
            validate_blast_database_available,
        )

        validate_blast_database_available(
            storage_account=storage_account, database=database
        )
        result = GateResult(
            id="blast_database",
            status="ok",
            severity="critical",
            error_code="",
            message=f"BLAST database '{database}' is available.",
        )
    except BlastDatabaseAvailabilityError as exc:
        code = str(getattr(exc, "code", "") or "database_not_found")
        # ``database_check_unavailable`` is the wrapper around Storage SDK errors;
        # treat that as "unknown" so caller-side override can pass through.
        status: GateStatus = "unknown" if code == "database_check_unavailable" else "fail"
        result = GateResult(
            id="blast_database",
            status=status,
            severity="critical",
            error_code=code,
            message=str(exc)[:300],
            action="Prepare the database",
            action_type="prepare_database",
        )
    except Exception as exc:
        LOGGER.warning("submit gate: DB probe failed: %s", type(exc).__name__)
        result = GateResult(
            id="blast_database",
            status="unknown",
            severity="critical",
            error_code="database_check_unavailable",
            message=f"Could not verify BLAST database ({type(exc).__name__}).",
        )
    _cache_set(cache_key, result)
    return result


def evaluate_submit_gates(
    *,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    storage_account: str,
    database: str,
    allow_unverified: bool = False,
) -> SubmitGatesReport:
    """Run every critical submit gate and return an aggregated report.

    ``allow_unverified=True`` downgrades any gate whose status is ``unknown``
    (i.e. could not be evaluated because of an upstream error) to ``warning``
    severity so it does not block the submit. Definitive ``fail`` results
    always block.
    """

    gates: list[GateResult] = [
        _gate_exec_token(),
        _gate_terminal_sidecar(),
        _gate_broker(),
        _gate_aks_cluster(
            subscription_id=subscription_id,
            resource_group=resource_group,
            cluster_name=cluster_name,
        ),
        _gate_blast_database(
            storage_account=storage_account,
            database=database,
        ),
    ]
    if allow_unverified:
        gates = [
            replace(g, severity="warning") if g.status == "unknown" else g
            for g in gates
        ]
    blocking = [g for g in gates if g.status != "ok" and g.severity == "critical"]
    return SubmitGatesReport(ok=not blocking, gates=gates, blocking=blocking)


__all__ = (
    "GateResult",
    "SubmitGatesReport",
    "evaluate_submit_gates",
    "reset_submit_gates_cache",
)
