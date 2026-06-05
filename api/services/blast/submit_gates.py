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
from collections.abc import Mapping
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

# Memory (GiB) ElasticBLAST reserves for the OS before fitting a database into a
# node. Mirrors ``SYSTEM_MEMORY_RESERVE`` in the sibling repo
# (``elastic-blast-azure`` src/elastic_blast/constants.py — currently an interim
# value of 2). ElasticBLAST's full-DB submit pre-flight rejects when
# ``node_ram_gib - SYSTEM_MEMORY_RESERVE < bytes_to_cache_gib``; the
# ``node_memory_fit`` gate subtracts the same value so its verdict matches
# ElasticBLAST's exactly. Keep in sync if the sibling repo bumps the constant
# (charter §13 cross-repo consistency); the SPA mirror lives in
# ``web/src/pages/blastSubmit/memoryFit.ts``.
_SYSTEM_MEMORY_RESERVE_GIB = 2.0


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


def _gate_openapi_ready() -> GateResult:
    """Pre-flight the sibling elb-openapi ``/v1/ready`` probe.

    Without this gate the internal ``POST /api/blast/submit`` route would
    fall through to the slow 90s ``submit_job`` httpx call when AKS is
    stopped — the same 90s opaque hang the external OpenAPI path eliminated
    with PR1. The gate reuses ``api.services.external_blast.ready`` so it
    shares the tiny in-process cache + 5s timeout + structured codes.

    Outcomes:

    * Sibling 200 / fail-open 404 → ``ok``.
    * Sibling 503 ``openapi_not_ready`` (any upstream_code) → ``fail`` with
      the upstream code mapped to a SPA action.
    * Sibling 429 ``openapi_ready_rate_limited`` → ``unknown`` (so
      ``allow_unverified=True`` can degrade it to a warning).
    * Transport error ``openapi_unreachable`` → ``fail`` with a "start
      cluster" action because the most common cause is AKS stopped.

    Skipped entirely when ``ELB_OPENAPI_BASE_URL`` is unset *and* the
    runtime cache has no endpoint — the cluster has no openapi sidecar
    deployed yet, which is a different failure mode owned by
    ``_gate_acr_images``.
    """
    try:
        from api.services.external_blast import _base_url, ready
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("submit gate: external_blast import failed: %s", exc)
        return GateResult(
            id="openapi_ready",
            status="unknown",
            severity="critical",
            error_code="openapi_check_unavailable",
            message="Could not import the openapi readiness client.",
        )
    try:
        _base_url()  # raises HTTPException if no base URL is configured
    except Exception:
        return GateResult(
            id="openapi_ready",
            status="ok",
            severity="critical",
            error_code="",
            message="elb-openapi not configured — skipped.",
        )
    try:
        ready()
        return GateResult(
            id="openapi_ready",
            status="ok",
            severity="critical",
            error_code="",
            message="elb-openapi /v1/ready passed all probes.",
        )
    except Exception as exc:
        # HTTPException is the canonical shape from ready(); fall back to
        # generic "unknown" for anything else.
        detail = getattr(exc, "detail", None)
        status_code = getattr(exc, "status_code", 503)
        if isinstance(detail, dict):
            code = str(detail.get("code") or "openapi_unreachable")
            upstream = str(detail.get("upstream_code") or "")
            message = str(detail.get("message") or "")
        else:
            code = "openapi_unreachable"
            upstream = ""
            message = str(detail or exc)[:300]

        if status_code == 429 or code == "openapi_ready_rate_limited":
            return GateResult(
                id="openapi_ready",
                status="unknown",
                severity="critical",
                error_code="openapi_ready_rate_limited",
                message=message or "Sibling /v1/ready is rate-limited; retry in 60s.",
            )

        # Map upstream code → SPA action hint.
        action, action_type = _openapi_action_for_code(upstream or code)
        return GateResult(
            id="openapi_ready",
            status="fail",
            severity="critical",
            error_code=code,
            message=message or f"elb-openapi readiness probe failed ({code}).",
            action=action,
            action_type=action_type,
        )


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


def _gate_node_memory_fit(
    *,
    storage_account: str,
    database: str,
    options: Mapping[str, Any] | None,
) -> GateResult:
    """For a full-database (non-sharded) BLAST, verify the database fits node RAM.

    ElasticBLAST's own submit pre-flight rejects a full-DB run whose memory
    requirement exceeds the workload node's RAM::

        ERROR: BLAST database .../core_nt memory requirements exceed memory
        available on selected machine type "Standard_E16s_v5". Please select
        machine type with at least 251.7GB available memory.

    The DB's ``bytes-to-cache`` (from the BLASTDB ``.njs`` metadata) is the exact
    number ElasticBLAST compares against the node RAM, so mirroring that
    comparison here neither false-blocks a DB ElasticBLAST would accept
    (e.g. core_nt 251.7 GB on Standard_E32s_v5 / 256 GB) nor lets through one it
    would reject. The sharded execution profile partitions the DB so each shard
    fits node memory — this gate only applies to the ``off`` (full-DB) profile.

    Non-blocking by design except for a definitive over-RAM verdict:

    * ``sharding_mode != "off"`` → ``ok`` (sharded path handles capacity).
    * ``bytes-to-cache`` unknown, or the node SKU's RAM is unknown → ``ok``
      (no authoritative number → do not false-block; ElasticBLAST's pre-flight
      and the post-failure guidance remain the safety net).
    * Storage / probe error → ``unknown`` + ``warning`` severity so it never
      blocks the submit.
    * Requirement exceeds node RAM → ``fail`` + ``critical`` (blocks, steers to
      the Sharded throughput execution profile or a larger cluster).

    Cached per (storage_account, database, machine_type) for 5s like the other
    data-plane gates.
    """
    opts = options or {}
    machine_type = str(opts.get("machine_type") or "")
    # Resolve the sharding mode through the SAME normaliser the INI generator
    # uses (``api.services.blast.config.generate_config`` → ``normalize_sharding_mode``)
    # so a caller that omits ``sharding_mode`` but sets ``db_auto_partition`` /
    # ``allow_approximate_sharding`` / ``db_partitions`` is treated as sharded
    # here too. Reading the raw ``sharding_mode`` string would mis-classify those
    # as ``off`` and false-block a run that actually auto-shards. Invalid option
    # combinations raise here; the INI generator will reject them with a precise
    # error, so skip the memory check rather than pre-empt it with a block.
    try:
        from api.services.sharding_precision import normalize_sharding_mode

        mode = normalize_sharding_mode(opts)
    except Exception:
        return GateResult(
            id="node_memory_fit",
            status="ok",
            severity="critical",
            error_code="",
            message="Sharding options invalid; full-DB memory check skipped.",
        )
    if mode != "off":
        return GateResult(
            id="node_memory_fit",
            status="ok",
            severity="critical",
            error_code="",
            message="Sharded execution profile — full-DB memory check skipped.",
        )
    cache_key = f"memfit:{storage_account}:{database}:{machine_type}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        from api.services.aks_skus import SKU_BY_NAME, normalize_sku_name
        from api.services.blast.db_metadata import (
            extract_db_name,
            resolve_blastdb_json_metadata,
        )

        db_name = extract_db_name(database)
        node_ram_gib: float | None = None
        if machine_type:
            sku = SKU_BY_NAME.get(normalize_sku_name(machine_type))
            if sku is not None:
                node_ram_gib = float(sku.memory_gib)

        if node_ram_gib is None or not db_name:
            # Unknown node RAM (or no DB name) — we cannot evaluate the fit, so
            # skip without a Storage round-trip and never block.
            result = GateResult(
                id="node_memory_fit",
                status="ok",
                severity="critical",
                error_code="",
                message=(
                    "Workload node memory is unknown; full-DB memory check skipped."
                ),
            )
            _cache_set(cache_key, result)
            return result

        meta = resolve_blastdb_json_metadata(storage_account, db_name)
        bytes_to_cache = int(meta.get("bytes_to_cache") or 0) if meta else 0

        if bytes_to_cache <= 0 or node_ram_gib is None:
            # No authoritative memory requirement, or unknown node RAM — do not
            # block (the user chose: never false-block on an unknown number).
            result = GateResult(
                id="node_memory_fit",
                status="ok",
                severity="critical",
                error_code="",
                message=(
                    "Full-DB memory requirement could not be determined; skipped."
                ),
            )
        else:
            # Mirror ElasticBLAST's submit pre-flight exactly
            # (elastic-blast-azure src/elastic_blast/elb_config.py: it rejects when
            # ``instance_props.memory - SYSTEM_MEMORY_RESERVE < bytes_to_cache_gb``).
            # Comparing against the raw node RAM would false-PASS a DB in the
            # ``(RAM - reserve, RAM]`` band that ElasticBLAST still rejects, so the
            # guard would not actually prevent the runtime error. Subtracting the
            # same reserve keeps us in lockstep — no false-block (we allow exactly
            # what ElasticBLAST allows) and no false-pass.
            required_gib = bytes_to_cache / float(1024**3)
            usable_gib = node_ram_gib - _SYSTEM_MEMORY_RESERVE_GIB
            if required_gib <= usable_gib:
                result = GateResult(
                    id="node_memory_fit",
                    status="ok",
                    severity="critical",
                    error_code="",
                    message=(
                        f"'{db_name}' needs {required_gib:.1f} GB and fits the "
                        f"{usable_gib:.0f} GB usable on a {node_ram_gib:.0f} GB "
                        "workload node."
                    ),
                )
            else:
                result = GateResult(
                    id="node_memory_fit",
                    status="fail",
                    severity="critical",
                    error_code="node_memory_insufficient",
                    message=(
                        f"'{db_name}' needs {required_gib:.1f} GB for a full-database "
                        "BLAST, which loads the entire database into a single node — "
                        "adding more nodes does not help. The workload node "
                        f"({machine_type}) provides only {usable_gib:.0f} GB usable "
                        f"({node_ram_gib:.0f} GB RAM minus "
                        f"{_SYSTEM_MEMORY_RESERVE_GIB:.0f} GB system reserve)."
                    ),
                    action="Switch to the Sharded throughput profile (or use a larger-SKU cluster)",
                    action_type="use_sharded_throughput",
                )
    except Exception as exc:
        LOGGER.warning("submit gate: node memory probe failed: %s", type(exc).__name__)
        # Advisory gate — a probe failure must not block submit (ElasticBLAST's
        # own pre-flight still catches a genuine over-RAM run). Warning severity
        # keeps it out of ``blocking``.
        result = GateResult(
            id="node_memory_fit",
            status="unknown",
            severity="warning",
            error_code="node_memory_check_unavailable",
            message=f"Could not verify node memory fit ({type(exc).__name__}).",
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


def _openapi_action_for_code(code: str) -> tuple[str | None, str | None]:
    """Map a sibling /v1/ready ``upstream_code`` to the SPA remediation hint.

    Kept separate from ``_readiness_action_for_code`` (which is DB-shaped) so
    the SPA can route each gate independently and a new upstream code does not
    silently collapse to the wrong action.
    """
    return OPENAPI_UPSTREAM_ACTIONS.get(code, (None, None))


# Canonical set of sibling ``/v1/ready`` upstream codes the dashboard knows
# how to remediate. Adding a new sibling code without an entry here makes the
# SPA fall back to a generic "Check AKS cluster health" hint — keep this in
# sync with both:
#   * sibling elastic-blast-azure/docker-openapi/app/main.py::v1_ready
#   * web/src/api/client.ts::OPENAPI_UPSTREAM_HINTS
# The contract test in api/tests/test_openapi_upstream_codes_contract.py
# fails the build when the SPA hint table drops a code that exists here.
#
# Single source of truth (critique #20.5): ``OPENAPI_UPSTREAM_ACTIONS``
# carries the (label, action_id) for every code we know how to remediate,
# including top-level dashboard-only wrappers like ``openapi_unreachable``.
# ``OPENAPI_NESTED_UPSTREAM_CODES`` is *derived* from that mapping by
# subtracting the explicit top-level wrappers, so adding a new sibling
# ``/v1/ready`` code only requires touching one place \u2014 the actions
# table \u2014 instead of two. Previously the two were hand-maintained in
# parallel and drift was caught only by the contract test, after the SPA
# already shipped.
OPENAPI_UPSTREAM_ACTIONS: dict[str, tuple[str, str]] = {
    "k8s_unreachable": ("Start cluster", "start_cluster"),
    "no_workload_nodes": ("Scale up workload pool", "scale_up_workload_pool"),
    "openapi_pod_not_ready": ("Restart elb-openapi", "restart_openapi_pod"),
    "workload_pool_check_failed": ("Check AKS health", "check_aks_health"),
    "openapi_pod_check_failed": ("Check AKS health", "check_aks_health"),
    "openapi_unreachable": ("Start cluster", "start_cluster"),
}

# Dashboard-only top-level codes that wrap transport / configuration
# failures BEFORE the sibling /v1/ready ever runs. These are handled by
# dedicated SPA branches, not the ``OPENAPI_UPSTREAM_HINTS`` hint table,
# so they MUST NOT appear in ``OPENAPI_NESTED_UPSTREAM_CODES``.
_OPENAPI_TOP_LEVEL_CODES: frozenset[str] = frozenset({"openapi_unreachable"})

OPENAPI_NESTED_UPSTREAM_CODES: frozenset[str] = frozenset(
    OPENAPI_UPSTREAM_ACTIONS.keys()
) - _OPENAPI_TOP_LEVEL_CODES


def openapi_known_upstream_codes() -> frozenset[str]:
    """Return the upstream codes the dashboard has a mapped remediation for.

    Public surface so the contract test (and any future doc generator) can
    cross-check this set against the SPA's hint table without poking at the
    private mapping table directly.

    Returns the *nested* upstream codes only (those arriving as
    ``detail.upstream_code``) so the SPA's hint table can be checked 1:1.
    Top-level dashboard-only codes like ``openapi_unreachable`` are handled
    by their own SPA branches and are intentionally excluded.
    """
    return OPENAPI_NESTED_UPSTREAM_CODES


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
    submit_options: Mapping[str, Any] | None = None,
    allow_unverified: bool = False,
) -> SubmitGatesReport:
    """Run every critical submit gate and return an aggregated report.

    ``allow_unverified=True`` downgrades any gate whose status is ``unknown``
    (i.e. could not be evaluated because of an upstream error) to ``warning``
    severity so it does not block the submit. Definitive ``fail`` results
    always block.

    ``submit_options`` carries the submit ``options`` dict (machine type +
    sharding flags). The node-memory-fit gate normalises the sharding mode from
    it the same way the INI generator does, so it skips the full-DB check for a
    sharded run; ``None`` simply skips that advisory check (it never
    false-blocks on missing inputs).
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
        _gate_openapi_ready(),
        _gate_blast_database(
            storage_account=storage_account,
            database=database,
        ),
        _gate_node_memory_fit(
            storage_account=storage_account,
            database=database,
            options=submit_options,
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
