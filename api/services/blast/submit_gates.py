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

# ACR repos the elastic-blast Kubernetes job manifests actually consume.
# `elb-openapi` is the SearchSP API surface and is independent of BLAST submit
# — intentionally excluded so a missing openapi image does not block a BLAST.
_BLAST_REQUIRED_REPOS = (
    "ncbi/elb",
    "ncbi/elasticblast-job-submit",
    "ncbi/elasticblast-query-split",
)


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
    marker blob under ``blast-db/<prefix>`` AND that prepare-db has finished
    writing it (``copy_status.phase == "completed"`` and no
    ``update_in_progress``). Cached per (storage_account, database) for 5s.
    Storage RBAC / network failures land as ``unknown``."""
    cache_key = f"db:{storage_account}:{database}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        from api.services.blast.task_config import (
            BlastDatabaseAvailabilityError,
            validate_blast_database_ready,
        )

        validate_blast_database_ready(
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
        action, action_type = _readiness_action_for_code(code)
        result = GateResult(
            id="blast_database",
            status=status,
            severity="critical",
            error_code=code,
            message=str(exc)[:300],
            action=action,
            action_type=action_type,
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


def _readiness_action_for_code(code: str) -> tuple[str | None, str | None]:
    """Map a readiness/availability error_code to the SPA's remediation hint."""
    if code == "database_not_ready":
        return ("Wait for download", "wait_for_download")
    if code == "database_updating":
        return ("Wait for update", "wait_for_update")
    return ("Prepare the database", "prepare_database")


def _gate_acr_images(*, acr_name: str) -> GateResult:
    """Verify every BLAST-pipeline image in ``IMAGE_TAGS`` resolves in the target ACR.

    When the ACR is empty (fresh deployment that never ran the build task)
    BLAST submit would otherwise enqueue, kick the Kubernetes job, and sit
    in ``ImagePullBackOff`` forever — the user sees an opaque ``queued``
    state with no actionable hint. Blocking up front lets the SPA render a
    "Build now" remediation that calls ``/api/acr/build-images`` directly.

    Only the three repos consumed by the elastic-blast Kubernetes job manifests
    (``ncbi/elb``, ``ncbi/elasticblast-job-submit``, ``ncbi/elasticblast-query-split``)
    are required here. ``elb-openapi`` is the SearchSP API surface and is
    independent of BLAST submit — gating on it would be over-strict.

    ``acr_name`` empty → ``unknown`` / ``warning`` (non-blocking) so submit
    flows that don't carry an ACR name are not bricked. Cached per (acr_name)
    for 5s like the other ARM/data-plane gates.
    """
    if not acr_name:
        return GateResult(
            id="acr_images",
            status="unknown",
            severity="warning",
            error_code="acr_not_configured",
            message="ACR name not provided; image presence cannot be verified.",
        )
    cache_key = f"acr_images:{acr_name}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        from api.services.image_tags import IMAGE_TAGS
        from api.services.upgrade.acr_inventory import lookup_images

        endpoint = f"{acr_name.strip().lower()}.azurecr.io"
        required = {
            repo: IMAGE_TAGS[repo]
            for repo in _BLAST_REQUIRED_REPOS
            if repo in IMAGE_TAGS
        }
        refs = [f"{endpoint}/{repo}:{tag}" for repo, tag in required.items()]
        infos = lookup_images(refs)
        missing = [info.image_ref for info in infos if not info.exists]
        unverifiable = any(
            not info.exists and info.error and "TagNotFound" not in info.error
            and "ManifestUnknown" not in info.error and "404" not in info.error
            for info in infos
        )
        if not missing:
            result = GateResult(
                id="acr_images",
                status="ok",
                severity="critical",
                error_code="",
                message=f"All {len(refs)} required image(s) are present in '{acr_name}'.",
            )
        elif unverifiable and len(missing) == len(refs):
            # Every lookup failed for a non-404 reason — likely RBAC or network,
            # not "actually missing". Downgrade to unknown so the user can
            # override with X-Submit-Allow-Unverified.
            result = GateResult(
                id="acr_images",
                status="unknown",
                severity="critical",
                error_code="acr_check_unavailable",
                message=f"Could not verify ACR images in '{acr_name}' (RBAC or network).",
            )
        else:
            short = ", ".join(ref.split("/", 1)[-1] for ref in missing[:3])
            extra = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
            result = GateResult(
                id="acr_images",
                status="fail",
                severity="critical",
                error_code="acr_images_missing",
                message=(
                    f"{len(missing)} required image(s) missing in '{acr_name}': "
                    f"{short}{extra}. Build them before submitting."
                ),
                action="Build ACR images",
                action_type="build_acr_images",
            )
    except Exception as exc:
        LOGGER.warning("submit gate: ACR probe failed: %s", type(exc).__name__)
        result = GateResult(
            id="acr_images",
            status="unknown",
            severity="critical",
            error_code="acr_check_unavailable",
            message=f"Could not verify ACR images ({type(exc).__name__}).",
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
    acr_name: str = "",
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
        _gate_acr_images(acr_name=acr_name),
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
