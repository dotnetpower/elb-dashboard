"""ElasticBLAST database warmup state inspection via the direct Kubernetes API.

Responsibility: Detect / release ElasticBLAST warmup resources and report DB warm state
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code. Session/credential seams (`_get_k8s_session`,
`_namespace_or_default`) stay in `monitoring` and are resolved lazily so tests can
monkeypatch them on that module.
Key entry points: `k8s_warmup_status`, `k8s_release_warmup_cache`,
`k8s_release_stale_warmup_jobs`, `k8s_check_namespace_exists`
Risky contracts: Use direct Kubernetes API helpers; do not reintroduce Azure Run Command.
The six top-level reads in `k8s_warmup_status` fan out via `_k8s_fanout_pool`; keep them
independent so the wall time stays bounded by the slowest call.
Validation: `uv run pytest -q api/tests/test_k8s_warmup_status_parallel.py
api/tests/test_k8s_release_stale_warmup_jobs.py`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from azure.core.credentials import TokenCredential

from api.services.k8s.fanout import _k8s_fanout_pool
from api.services.k8s.nodes import _candidate_warmup_node_names
from api.services.warmup.jobs import (
    DEFAULT_WARMUP_APP_LABEL,
    attach_pod_progress_to_database_status,
    database_status_from_warmup_jobs,
)

LOGGER = logging.getLogger(__name__)

_K8S_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")


def k8s_release_warmup_cache(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    db_name: str,
    namespace: str = "default",
) -> dict[str, Any]:
    """Release node-local warmup resources for one database.

    The operation removes the Kubernetes resources that keep the dashboard's
    warm-cache state alive. Node-local kernel/page cache may drain gradually,
    but subsequent status checks no longer report the DB as warmed.
    """

    from api.services.k8s.monitoring import _get_k8s_session, _namespace_or_default

    db_label = _warmup_db_label_value(db_name)
    if not _K8S_LABEL_VALUE_RE.match(db_label):
        raise ValueError("db_name is not a valid Kubernetes label value")

    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    try:
        target_ns = _namespace_or_default(session, server, namespace)
        deleted: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        targets = [
            (
                "jobs",
                f"{server}/apis/batch/v1/namespaces/{target_ns}/jobs",
                f"app={DEFAULT_WARMUP_APP_LABEL},db={db_label}",
            ),
            (
                "legacy-daemonsets",
                f"{server}/apis/apps/v1/namespaces/{target_ns}/daemonsets",
                f"app=db-warmup,db={db_label}",
            ),
        ]

        for kind, url, selector in targets:
            response = session.delete(
                url,
                params={"labelSelector": selector, "propagationPolicy": "Background"},
                timeout=10,
            )
            item = {"kind": kind, "selector": selector, "status_code": response.status_code}
            if response.status_code in (200, 201, 202, 404):
                deleted.append(item)
            else:
                errors.append({**item, "detail": response.text[:200]})

        return {
            "status": "released" if not errors else "partial",
            "database": db_name,
            "db_label": db_label,
            "namespace": target_ns,
            "deleted": deleted,
            "errors": errors,
        }
    finally:
        session.close()


def k8s_release_stale_warmup_jobs(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    db_name: str,
    current_node_names: Iterable[str],
    namespace: str = "default",
    current_source_version: str = "",
) -> dict[str, Any]:
    """Delete warmup Jobs (and their pods) pinned to stale nodes or generations.

    ``Job.spec.template.spec.nodeName`` is immutable, so when AKS stop/start
    rotates VMSS instances the dashboard's previously-succeeded warmup Jobs
    cannot run again on the replacement nodes — they sit at ``succeeded=1``
    forever while ``_mark_stale_warmup_nodes`` correctly flags the DB as
    ``Stale``. Re-running ``k8s_ensure_job_manifests`` won't help either,
    because the existing Job names collide and ensure skips them.

    This helper finds Jobs labelled ``app=db-warmup, db=<name>`` whose pinned
    ``nodeName`` is not in ``current_node_names`` or whose source-version
    annotation does not match ``current_source_version`` and deletes them with
    ``propagationPolicy=Background`` so the pods clean up too. The next
    ``k8s_ensure_job_manifests`` call will then recreate fresh Jobs on the
    current ready nodes and DB generation.
    """

    from api.services.k8s.monitoring import _get_k8s_session, _namespace_or_default

    db_label = _warmup_db_label_value(db_name)
    if not _K8S_LABEL_VALUE_RE.match(db_label):
        raise ValueError("db_name is not a valid Kubernetes label value")

    live_nodes = {str(name) for name in current_node_names if name}

    session, server = _get_k8s_session(
        credential, subscription_id, resource_group, cluster_name, admin=True
    )
    try:
        target_ns = _namespace_or_default(session, server, namespace)
        list_url = f"{server}/apis/batch/v1/namespaces/{target_ns}/jobs"
        response = session.get(
            list_url,
            params={"labelSelector": f"app={DEFAULT_WARMUP_APP_LABEL},db={db_label}"},
            timeout=10,
        )
        if response.status_code != 200:
            return {
                "status": "error",
                "database": db_name,
                "namespace": target_ns,
                "status_code": response.status_code,
                "detail": response.text[:200],
            }

        deleted: list[dict[str, Any]] = []
        kept: list[str] = []
        errors: list[dict[str, Any]] = []
        for job in response.json().get("items", []):
            metadata = job.get("metadata", {}) or {}
            name = str(metadata.get("name") or "")
            if not name:
                continue
            pinned = job.get("spec", {}).get("template", {}).get("spec", {}).get("nodeName") or ""
            metadata_annotations = metadata.get("annotations", {}) or {}
            template_metadata = job.get("spec", {}).get("template", {}).get("metadata", {}) or {}
            template_annotations = template_metadata.get("annotations", {}) or {}
            source_version = str(
                metadata_annotations.get("elb.dashboard/source-version")
                or template_annotations.get("elb.dashboard/source-version")
                or ""
            )
            source_stale = bool(current_source_version and source_version != current_source_version)
            node_stale = bool(pinned and str(pinned) not in live_nodes)
            if not node_stale and not source_stale:
                kept.append(name)
                continue
            del_response = session.delete(
                f"{list_url}/{name}",
                params={"propagationPolicy": "Background"},
                timeout=10,
            )
            if del_response.status_code in (200, 201, 202, 404):
                deleted.append(
                    {
                        "name": name,
                        "stale_node": str(pinned) if node_stale else "",
                        "stale_source_version": source_version if source_stale else "",
                        "current_source_version": current_source_version if source_stale else "",
                    }
                )
            else:
                errors.append(
                    {
                        "name": name,
                        "stale_node": str(pinned) if node_stale else "",
                        "stale_source_version": source_version if source_stale else "",
                        "current_source_version": current_source_version if source_stale else "",
                        "status_code": del_response.status_code,
                        "detail": del_response.text[:200],
                    }
                )

        return {
            "status": "released" if not errors else "partial",
            "database": db_name,
            "namespace": target_ns,
            "deleted": deleted,
            "kept": kept,
            "errors": errors,
        }
    finally:
        session.close()


def _warmup_db_label_value(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-_.")
    if not label:
        return "db"
    return label[:63].rstrip("-_.") or "db"


def k8s_check_namespace_exists(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    namespace: str,
) -> bool:
    """Return whether ElasticBLAST warmup resources appear to exist."""

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        response = session.get(
            f"{server}/apis/apps/v1/namespaces/kube-system/daemonsets/create-workspace",
            timeout=10,
        )
        if response.status_code == 200:
            ready = response.json().get("status", {}).get("numberReady", 0)
            if ready > 0:
                return True

        response = session.get(f"{server}/api/v1/namespaces/default/pods", timeout=10)
        if response.status_code != 200:
            return False
        pods = response.json().get("items", [])
        return any(
            "vmtouch" in pod.get("metadata", {}).get("name", "")
            or "elb" in pod.get("metadata", {}).get("name", "")
            for pod in pods
        )
    except Exception:
        return False
    finally:
        session.close()


def k8s_warmup_status(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
) -> dict[str, Any]:
    """Detect warmup state by inspecting ElasticBLAST Kubernetes resources.

    The six top-level GETs are independent and fan out via a thread pool so
    the total wall time is bounded by the slowest call instead of the sum
    of all calls. ``requests.Session`` is thread-safe for concurrent
    requests (it's just a connection pool + cookie jar — we don't mutate
    session state on these read paths).
    """

    from api.services.k8s.monitoring import _get_k8s_session

    session, server = _get_k8s_session(credential, subscription_id, resource_group, cluster_name)
    try:
        result: dict[str, Any] = {
            "warm": False,
            "workspace_ready": 0,
            "workspace_desired": 0,
            "databases": [],
            "vmtouch_ready": 0,
            "namespaces": [],
        }

        # Phase 1 — fan out the six independent reads in parallel.
        def _get(url: str, params: dict[str, str] | None = None) -> Any:
            return session.get(url, params=params, timeout=10)

        # Reuses the process-wide ``_k8s_fanout_pool`` so we do not
        # spawn + tear down 6 worker threads on every monitor poll.
        # Submitted futures' exceptions are re-raised inside ``.result()``
        # which we already let bubble to the outer try/except below.
        pool = _k8s_fanout_pool()
        f_workspace = pool.submit(
            _get,
            f"{server}/apis/apps/v1/namespaces/kube-system/daemonsets/create-workspace",
        )
        f_vmtouch = pool.submit(
            _get,
            f"{server}/apis/apps/v1/namespaces/default/daemonsets/vmtouch-db-cache",
        )
        f_setup_jobs = pool.submit(
            _get,
            f"{server}/apis/batch/v1/namespaces/default/jobs",
            {"labelSelector": "app=setup"},
        )
        f_warmup_jobs = pool.submit(
            _get,
            f"{server}/apis/batch/v1/namespaces/default/jobs",
            {"labelSelector": f"app={DEFAULT_WARMUP_APP_LABEL}"},
        )
        f_warmup_ds = pool.submit(
            _get,
            f"{server}/apis/apps/v1/namespaces/default/daemonsets",
            {"labelSelector": "app=db-warmup"},
        )
        f_namespaces = pool.submit(_get, f"{server}/api/v1/namespaces")

        response = f_workspace.result()
        if response.status_code == 200:
            status = response.json().get("status", {})
            result["workspace_ready"] = status.get("numberReady", 0)
            result["workspace_desired"] = status.get("desiredNumberScheduled", 0)
            result["warm"] = result["workspace_ready"] > 0

        response = f_vmtouch.result()
        if response.status_code == 200:
            result["vmtouch_ready"] = response.json().get("status", {}).get("numberReady", 0)
            result["warm"] = result["warm"] or result["vmtouch_ready"] > 0

        response = f_setup_jobs.result()
        if response.status_code == 200:
            result["databases"] = _database_status_from_setup_jobs(
                response.json().get("items", [])
            )

        response = f_warmup_jobs.result()
        if response.status_code == 200:
            warmup_databases = database_status_from_warmup_jobs(
                response.json().get("items", [])
            )
            # Phase 2 — node-pinning check and pod-log fetch are independent
            # of each other and of the remaining Phase 1 results, so fan
            # them out too. Pod log fetches are themselves parallelised
            # inside `_warmup_pods_and_logs`.
            f_stale = pool.submit(
                _mark_stale_warmup_nodes, session, server, warmup_databases
            )
            f_pods = pool.submit(_warmup_pods_and_logs, session, server)
            f_stale.result()
            pods, logs_by_pod = f_pods.result()
            attach_pod_progress_to_database_status(warmup_databases, pods, logs_by_pod)
            _merge_database_statuses(result, warmup_databases)

        response = f_warmup_ds.result()
        if response.status_code == 200:
            _append_warmup_daemonsets(result, response.json().get("items", []))

        response = f_namespaces.result()
        if response.status_code == 200:
            result["namespaces"] = [
                namespace_item.get("metadata", {}).get("name", "")
                for namespace_item in response.json().get("items", [])
                if namespace_item.get("metadata", {}).get("name", "").startswith(
                    "elastic-blast-"
                )
            ][:20]

        return result
    except Exception as exc:
        # Dashboard polls warmup status every few seconds, so a sustained
        # AKS read-timeout / DNS hiccup would emit a fresh WARNING per
        # tick without dedup. Key by (cluster, exc class) so a new error
        # class still surfaces; repeats drop to DEBUG.
        from api.services.log_dedup import dedup_log_warning

        dedup_log_warning(
            LOGGER,
            ("k8s_warmup_status", cluster_name, type(exc).__name__),
            "k8s_warmup_status failed for %s: %s",
            cluster_name,
            str(exc)[:200],
        )
        return {
            "warm": False,
            "workspace_ready": 0,
            "workspace_desired": 0,
            "databases": [],
            "vmtouch_ready": 0,
            "namespaces": [],
            "error": str(exc)[:200],
        }
    finally:
        session.close()


def _database_status_from_setup_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    db_map: dict[str, dict[str, Any]] = {}
    for job in jobs:
        job_name = job.get("metadata", {}).get("name", "")
        if not job_name.startswith("init-ssd-"):
            continue

        db_name = ""
        mol_type = ""
        containers = job.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            for env in container.get("env", []):
                if env.get("name") == "ELB_DB":
                    db_name = env.get("value", "")
                elif env.get("name") == "ELB_DB_MOL_TYPE":
                    mol_type = env.get("value", "")
        if not db_name:
            continue
        shard = ""
        shard_match = re.match(r"^(?P<db>.+)_shard_(?P<shard>\d{2,})$", str(db_name))
        if shard_match:
            db_name = shard_match.group("db")
            shard = shard_match.group("shard")

        info = db_map.setdefault(
            db_name,
            {
                "name": db_name,
                "mol_type": mol_type,
                "nodes_ready": 0,
                "nodes_failed": 0,
                "nodes_active": 0,
                "total_jobs": 0,
                "shards": [],
                # `setup` means the entry was derived from an ElasticBLAST
                # `init-ssd-*` submit-side stager Job, not from an explicit
                # dashboard warmup. The New Search run-profile picker uses
                # this to avoid auto-flipping to "Warmed database" just
                # because a prior BLAST submit happened to cache the DB.
                "sources": ["setup"],
            },
        )
        job_status = job.get("status", {})
        info["total_jobs"] += 1
        info["nodes_ready"] += job_status.get("succeeded", 0)
        info["nodes_failed"] += job_status.get("failed", 0)
        info["nodes_active"] += job_status.get("active", 0)
        if shard:
            info["shards"].append(shard)

    for info in db_map.values():
        info["shards"] = sorted(set(info.get("shards", [])))
        total = info["total_jobs"]
        if info["nodes_ready"] == total and total > 0:
            info["status"] = "Ready"
        elif info["nodes_active"] > 0:
            info["status"] = "Loading"
        elif info["nodes_failed"] > 0:
            info["status"] = "Failed"
        else:
            info["status"] = "Unknown"
    return list(db_map.values())


def _merge_database_statuses(result: dict[str, Any], incoming: list[dict[str, Any]]) -> None:
    existing = {database["name"]: database for database in result["databases"]}
    for database in incoming:
        name = database.get("name")
        if not name:
            continue
        if name in existing:
            current = existing[name]
            incoming_is_warmup = "warmup" in (database.get("sources") or [])
            current_has_warmup = "warmup" in (current.get("sources") or [])
            # Node-local warmup Jobs (one per Ready node) are the authoritative
            # denominator for the dashboard's AKS cache state. ElasticBLAST
            # submit-side `init-ssd-*` setup Jobs use their own internal shard
            # count (often greater than the node count, e.g. ~20 for core_nt on
            # 10 nodes), so a plain `max` merge would inflate the denominator
            # (10 nodes shown as "10/20") and wrongly hold BLAST submit at
            # `warmup_not_ready`. When the incoming warmup-Job entry overrides a
            # setup-only entry, take its counts and status verbatim.
            warmup_authoritative = incoming_is_warmup and not current_has_warmup
            for key in ("nodes_ready", "nodes_failed", "nodes_active", "total_jobs"):
                if warmup_authoritative:
                    current[key] = int(database.get(key) or 0)
                else:
                    current[key] = max(int(current.get(key) or 0), int(database.get(key) or 0))
            if warmup_authoritative:
                current["status"] = database.get("status", current.get("status", "Unknown"))
            elif database.get("status") == "Ready" or current.get("status") != "Ready":
                current["status"] = database.get("status", current.get("status", "Unknown"))
            if database.get("shards"):
                current["shards"] = sorted(
                    set(current.get("shards", [])) | set(database.get("shards", []))
                )
            incoming_sources = database.get("sources") or []
            if incoming_sources:
                current["sources"] = sorted(
                    set(current.get("sources", [])) | set(incoming_sources)
                )
            for key in (
                "progress_pct",
                "started_at",
                "elapsed_seconds",
                "estimated_remaining_seconds",
                "active_phase",
                "active_phase_label",
                "phase_counts",
                "pod_statuses",
                "shard_nodes",
                "shard_host_paths",
                # `source_version`/`source_versions` are the DB-generation
                # marker the BLAST submit gate (`ensure_node_warmup_ready_for
                # _submit`) compares against the storage blob's
                # `source_version`. Only warmup Jobs carry it (setup
                # `init-ssd-*` Jobs do not), so when a setup-derived entry is
                # created first and the warmup entry is merged in afterwards it
                # MUST carry the marker across — otherwise the merged entry is
                # `status="Ready"` but marker-less, and submit fails with
                # "node warmup for <db> has no DB generation marker" even
                # though the dashboard card shows the DB as warm.
                "source_version",
                "source_versions",
            ):
                if key in database:
                    current[key] = database[key]
        else:
            result["databases"].append(database)
            existing[name] = database
        if database.get("status") == "Ready":
            result["warm"] = True


def _mark_stale_warmup_nodes(
    session: Any,
    server: str,
    databases: list[dict[str, Any]],
) -> None:
    response = session.get(f"{server}/api/v1/nodes", timeout=10)
    if response.status_code != 200:
        return
    ready_nodes = set(_candidate_warmup_node_names(response.json().get("items", [])))
    if not ready_nodes:
        return
    for database in databases:
        shard_nodes = database.get("shard_nodes") or {}
        if not isinstance(shard_nodes, dict):
            continue
        stale_shards = sorted(
            shard for shard, node_name in shard_nodes.items() if str(node_name) not in ready_nodes
        )
        if not stale_shards:
            continue
        database["status"] = "Stale"
        database["nodes_active"] = 0
        database["nodes_ready"] = 0
        database["nodes_failed"] = int(database.get("total_jobs") or len(stale_shards))
        database["stale_shards"] = stale_shards
        database["active_phase"] = "failed"
        database["active_phase_label"] = "Warmup stale"
        database["active_message"] = "Warmup jobs are pinned to nodes that are no longer Ready."


def _warmup_pods_and_logs(session: Any, server: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    response = session.get(
        f"{server}/api/v1/namespaces/default/pods",
        params={"labelSelector": f"app={DEFAULT_WARMUP_APP_LABEL}"},
        timeout=10,
    )
    if response.status_code != 200:
        return [], {}
    pods = response.json().get("items", [])
    pod_names = [
        pod.get("metadata", {}).get("name", "")
        for pod in pods[:12]
        if pod.get("metadata", {}).get("name")
    ]
    if not pod_names:
        return pods, {}

    def _fetch_log(name: str) -> tuple[str, str | None]:
        try:
            log_response = session.get(
                f"{server}/api/v1/namespaces/default/pods/{name}/log",
                params={"container": "warmup", "tailLines": 80},
                timeout=2,
            )
        except Exception:
            return name, None
        if log_response.status_code != 200:
            return name, None
        return name, log_response.text[-8000:]

    # Up to 12 pod log GETs — fire concurrently so the wall time is bounded
    # by the slowest log fetch (2 s timeout each) instead of summing all 12.
    # Reuses the process-wide ``_k8s_fanout_pool`` so we do not spawn +
    # tear down 12 worker threads on every monitor poll.
    logs_by_pod: dict[str, str] = {}
    if pod_names:
        for name, text in _k8s_fanout_pool().map(_fetch_log, pod_names):
            if text is not None:
                logs_by_pod[name] = text
    return pods, logs_by_pod


def _append_warmup_daemonsets(result: dict[str, Any], daemonsets: list[dict[str, Any]]) -> None:
    existing_db_names = {database["name"] for database in result["databases"]}
    for daemonset in daemonsets:
        db_label = daemonset.get("metadata", {}).get("labels", {}).get("db", "")
        if not db_label or db_label in existing_db_names:
            continue
        status = daemonset.get("status", {})
        desired = status.get("desiredNumberScheduled", 0)
        ready = status.get("numberReady", 0)
        if desired == 0:
            continue
        result["databases"].append(
            {
                "name": db_label,
                "mol_type": "",
                "nodes_ready": ready,
                "nodes_failed": 0,
                "nodes_active": desired - ready,
                "total_jobs": desired,
                "status": "Ready" if ready == desired else "Loading",
                "sources": ["warmup"],
            }
        )
        if ready > 0:
            result["warm"] = True
