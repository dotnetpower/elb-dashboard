"""BLAST submit pre-flight checks.

Responsibility: BLAST submit pre-flight checks
Edit boundaries: Keep HTTP validation and response shaping here; move cloud/data-plane work into
services or tasks.
Key entry points: `blast_pre_flight`
Risky contracts: Every non-health `/api/*` route must enforce `require_caller` or an equivalent
auth gate.
Validation: `uv run pytest -q api/tests/test_blast_results_routes.py
api/tests/test_route_contracts.py`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from api.auth import CallerIdentity, require_caller
from api.routes._blast_shared import _apply_web_blast_searchsp_default
from api.services.response_contracts import (
    AdmissionDecision,
    build_admission,
    build_meta,
    request_id_from_scope,
)
from api.services.sanitise import sanitise

router = APIRouter()


@router.post("/pre-flight")
def blast_pre_flight(
    request: Request,
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Run pre-flight checks before BLAST submit.

    Pre-flight is a read-only admission simulation: HTTP 200 means the
    simulation completed, while `decision` tells the caller whether a matching
    submit would be accepted at this point-in-time snapshot.
    """

    checks: list[dict[str, Any]] = []
    critical = 0

    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    cluster = body.get("cluster_name") or body.get("aks_cluster_name") or ""
    storage = body.get("storage_account", "")
    db = body.get("db") or body.get("database", "")
    compatibility_contract: dict[str, Any] | None = None
    _raw_opts = body.get("options")
    raw_options: dict[str, Any] = _raw_opts if isinstance(_raw_opts, dict) else {}
    precision_options = {**raw_options}
    for key in (
        "additional_options",
        "allow_approximate_sharding",
        "db_auto_partition",
        "db_partitions",
        "db_partition_prefix",
        "db_effective_search_space",
        "db_total_letters",
        "outfmt",
        "query_effective_search_spaces",
        "searchsp",
        "sharding_mode",
    ):
        if key in body:
            if key == "searchsp":
                precision_options.setdefault("db_effective_search_space", body[key])
            else:
                precision_options[key] = body[key]
    _apply_web_blast_searchsp_default(str(db), precision_options)

    # Local sidecar gates from `api.services.blast.submit_gates`: terminal
    # sidecar reachability and EXEC_TOKEN presence are blocking conditions for
    # the submit task but were previously invisible in pre-flight, so the SPA
    # could not warn the user before they clicked Run BLAST. Surface them here
    # in the same `checks[]` shape the existing UI already renders.
    try:
        from api.services.blast import submit_gates

        for gate_fn in (submit_gates._gate_exec_token, submit_gates._gate_terminal_sidecar):
            gate = gate_fn()
            checks.append(
                {
                    "id": gate.id,
                    "status": "pass" if gate.status == "ok" else "fail",
                    "title": (
                        "Terminal Sidecar"
                        if gate.id == "terminal_sidecar"
                        else "Exec Token"
                    ),
                    "detail": gate.message,
                    "severity": "critical" if gate.status != "ok" else None,
                    "action": gate.action,
                    "action_type": gate.action_type,
                }
            )
            if gate.status != "ok":
                critical += 1
    except Exception as exc:
        checks.append(
            {
                "id": "submit_gates",
                "status": "warn",
                "title": "Submit Gates",
                "detail": f"Could not evaluate sidecar gates: {type(exc).__name__}",
            }
        )

    # ACR images gate: a fresh deployment that never ran the build task has an
    # empty registry, which makes BLAST submits sit in `queued` forever
    # because the Kubernetes pod hits ImagePullBackOff. Surface it here with a
    # `build_acr_images` action so the SPA can offer a one-click remediation.
    acr_name = str(body.get("acr_name") or "")
    acr_rg = str(body.get("acr_resource_group") or "") or rg
    try:
        from api.services.blast import submit_gates

        acr_gate = submit_gates._gate_acr_images(acr_name=acr_name)
        if acr_gate.status == "ok":
            acr_status = "pass"
        elif acr_gate.status == "fail":
            acr_status = "fail"
        else:
            acr_status = "warn"
        check_row: dict[str, Any] = {
            "id": "acr_images",
            "status": acr_status,
            "title": "ACR Images",
            "detail": acr_gate.message,
            "severity": "critical" if acr_gate.status == "fail" else None,
            "action": acr_gate.action,
            "action_type": acr_gate.action_type,
        }
        if acr_gate.action_type == "build_acr_images":
            check_row["action_params"] = {
                "subscription_id": str(sub),
                "resource_group": acr_rg,
                "registry_name": acr_name,
            }
        checks.append(check_row)
        if acr_gate.status == "fail":
            critical += 1
    except Exception as exc:
        checks.append(
            {
                "id": "acr_images",
                "status": "warn",
                "title": "ACR Images",
                "detail": f"Could not verify ACR images: {type(exc).__name__}",
            }
        )

    try:
        from api.services import get_credential
        from api.services.monitoring import list_aks_clusters

        cred = get_credential()
        clusters = list_aks_clusters(cred, sub, rg)
        found = next((c for c in clusters if c.get("name") == cluster), None)
        if not found:
            checks.append(
                {
                    "id": "aks_cluster",
                    "status": "fail",
                    "title": "AKS Cluster",
                    "detail": f"Cluster '{cluster}' not found in '{rg}'",
                    "severity": "critical",
                }
            )
            critical += 1
        elif found.get("power_state") != "Running":
            checks.append(
                {
                    "id": "aks_cluster",
                    "status": "fail",
                    "title": "AKS Cluster",
                    "detail": f"Cluster is {found.get('power_state', 'unknown')}. Start it first.",
                    "severity": "critical",
                    "action": "Start cluster",
                    "action_type": "start_cluster",
                }
            )
            critical += 1
        else:
            checks.append(
                {
                    "id": "aks_cluster",
                    "status": "pass",
                    "title": "AKS Cluster",
                    "detail": f"{cluster} is running ({found.get('node_count', '?')} nodes)",
                }
            )
    except Exception as exc:
        checks.append(
            {
                "id": "aks_cluster",
                "status": "warn",
                "title": "AKS Cluster",
                "detail": f"Could not verify: {type(exc).__name__}",
            }
        )

    if storage:
        checks.append(
            {
                "id": "storage",
                "status": "pass",
                "title": "Storage Account",
                "detail": f"{storage} configured",
            }
        )
    else:
        checks.append(
            {
                "id": "storage",
                "status": "fail",
                "title": "Storage Account",
                "detail": "No storage account configured",
                "severity": "critical",
            }
        )
        critical += 1

    if db and storage:
        try:
            from api.services.blast.task_config import validate_blast_database_ready

            availability = validate_blast_database_ready(
                storage_account=str(storage),
                database=str(db),
            )
            checks.append(
                {
                    "id": "database",
                    "status": "pass",
                    "title": "BLAST Database",
                    "detail": f"Database '{db}' is available ({availability['marker_blob']})",
                }
            )
        except Exception as exc:
            # Map readiness vs missing to distinct labels so the SPA can
            # render the right remediation hint without parsing the message.
            code = getattr(exc, "code", "") or "database_not_found"
            if code == "database_not_ready":
                title = "BLAST Database Preparing"
            elif code == "database_updating":
                title = "BLAST Database Updating"
            else:
                title = "BLAST Database"
            checks.append(
                {
                    "id": "database",
                    "status": "fail",
                    "title": title,
                    "detail": sanitise(str(exc))[:300],
                    "severity": "critical",
                    "error_code": str(code),
                }
            )
            critical += 1
    elif db:
        checks.append(
            {
                "id": "database",
                "status": "pass",
                "title": "BLAST Database",
                "detail": f"Database '{db}' selected",
            }
        )
    else:
        checks.append(
            {
                "id": "database",
                "status": "fail",
                "title": "BLAST Database",
                "detail": "No database selected",
                "severity": "critical",
            }
        )
        critical += 1

    try:
        from api.services.blast.compatibility import build_compatibility_contract
        from api.services.sharding_precision import build_precision_report

        query_metadata = None
        query_count = body.get("query_count")
        query_data = body.get("query_data")
        if isinstance(query_data, str) and query_data.strip():
            from api.services.query_metadata import parse_fasta_metadata

            query_metadata = parse_fasta_metadata(query_data)
            query_count = query_metadata.query_count
        elif not isinstance(query_count, int):
            query_count = None
        shard_sets = body.get("shard_sets")
        if not isinstance(shard_sets, list):
            shard_sets = None
        precision_report = build_precision_report(
            precision_options,
            query_count=query_count,
            db_stats_available=bool(precision_options.get("db_total_letters")),
            shard_sets=shard_sets,
        )
        contract = build_compatibility_contract(
            database=str(db),
            options=precision_options,
            precision_report=precision_report,
        )
        compatibility_contract = contract.as_dict()
        status = "pass" if precision_report.eligible else "fail"
        checks.append(
            {
                "id": "sharding_precision",
                "status": status,
                "title": "Sharding Precision",
                "detail": precision_report.precision_level,
                "severity": "critical" if not precision_report.eligible else None,
                "precision": precision_report.as_dict(),
                "query_metadata": query_metadata.as_dict() if query_metadata else None,
            }
        )
        if not precision_report.eligible:
            critical += 1
        contract_status = (
            "fail" if not contract.eligible else "warn" if contract.mode != "precise" else "pass"
        )
        checks.append(
            {
                "id": "web_blast_compatibility",
                "status": contract_status,
                "title": "Web BLAST Compatibility",
                "detail": contract.level,
                "severity": "critical" if not contract.eligible else None,
                "compatibility": compatibility_contract,
            }
        )
        if not contract.eligible:
            critical += 1
    except Exception as exc:
        checks.append(
            {
                "id": "sharding_precision",
                "status": "fail",
                "title": "Sharding Precision",
                "detail": str(exc)[:200],
                "severity": "critical",
            }
        )
        critical += 1

    try:
        from api.celery_app import celery_app

        conn = celery_app.connection()
        conn.ensure_connection(max_retries=1, timeout=2)
        conn.close()
        checks.append(
            {
                "id": "broker",
                "status": "pass",
                "title": "Task Broker",
                "detail": "Redis is reachable",
            }
        )
    except Exception:
        checks.append(
            {
                "id": "broker",
                "status": "fail",
                "title": "Task Broker",
                "detail": "Redis is not reachable. Tasks cannot be queued.",
                "severity": "critical",
            }
        )
        critical += 1

    ready = critical == 0
    warnings = [check for check in checks if check.get("status") == "warn"]
    decision: AdmissionDecision = "would_accept" if ready else "would_reject"
    return {
        "status": "ok",
        "ready": ready,
        "decision": decision,
        "checks": checks,
        "compatibility": compatibility_contract,
        "critical_blockers": critical,
        "summary": "All checks passed" if ready else f"{critical} critical issue(s) found",
        "admission": build_admission(
            decision=decision,
            reason="preflight_checks_passed" if ready else "preflight_checks_blocked_submit",
            queue={"state": decision, "depth_bucket": "unknown"},
            capacity={"classification": "not_evaluated"},
            warnings=warnings,
        ),
        "meta": build_meta(request_id=request_id_from_scope(request), warnings=warnings),
    }
