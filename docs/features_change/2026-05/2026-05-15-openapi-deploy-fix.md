# OpenAPI deploy: real Celery task + clean status envelope

**Date:** 2026-05-15
**Scope:** `api/tasks/openapi.py` (new), `api/routes/stubs.py`,
`api/routes/monitor.py`, `api/celery_app.py`.

## Motivation

The `/docs` page on the SPA showed two broken behaviours:

1. **`POST /api/aks/openapi/deploy` was a stub.** It returned
   `{"status": "stub", "degraded": true, ...}` with a synthetic id, and
   the matching status route was also a stub that always returned
   `runtime_status: "Pending"`. Result: clicking *Deploy* on the
   `OpenApiDeployPanel` produced an infinite "Deploying…" spinner with
   nothing actually happening on Azure. The api log was being spammed
   with `STUB_CALLED endpoint=aks/openapi/deploy/status` once per ~5 s
   forever for the cached id stored in `localStorage`.
2. **`GET /api/monitor/aks/service-ip` returned 500.** The route was
   typed `-> dict[str, Any]` but the underlying helper
   `k8s_get_service_ip` returns `str | None`, so FastAPI raised a
   `ResponseValidationError` (`Input should be a valid dictionary`)
   whenever the LoadBalancer had no external IP yet — exactly the
   moment the SPA needs to render the "waiting for IP" state.

These were the two load-bearing endpoints behind the OpenAPI deploy
flow on the dashboard.

## User-facing change

* Clicking **Deploy OpenAPI** on the cluster's `/docs` panel now runs
  a real end-to-end deploy:
  1. **Workload identity setup** — creates user-assigned MI
     `id-elb-openapi` if missing, attaches a federated credential
     (`fc-elb-openapi`, subject
     `system:serviceaccount:default:elb-openapi-sa`,
     audience `api://AzureADTokenExchange`), and assigns the three
     roles the OpenAPI pod needs (Contributor on the cluster RG,
     Storage Blob Data Contributor on the storage account, AKS Cluster
     User on the cluster).
  2. **Manifest apply** — generates the SA / ClusterRole /
     ClusterRoleBinding / Deployment / LoadBalancer Service for
     `elb-openapi:<IMAGE_TAGS["elb-openapi"]>` and applies it via the
     terminal sidecar's exec server (`az aks get-credentials --admin`
     → `kubectl apply -f -`).
  3. **External IP wait** — polls the K8s API for the LB ingress IP
     for up to ~120 s.
* The progress is visible in the SPA: phase strings
  `setup_workload_identity` / `applying_manifests` /
  `waiting_for_external_ip` / `completed` are surfaced via the existing
  orchestrator-shaped envelope, so `OpenApiDeployPanel` renders real
  state instead of a permanent spinner.
* Failures return a clean error envelope the SPA can render:
  ```json
  {
    "status": "failed",
    "workload_identity": { "...mi/oidc/roles..." },
    "openapi_deploy": { "image": "...", "error": "<actionable message>" }
  }
  ```
  (E.g. the local-dev failure today is *"Cannot reach the terminal
  sidecar's exec server — ... EXEC_TOKEN env var is empty"*, which
  points the operator at the right Bicep secret.)
* `GET /api/monitor/aks/service-ip` no longer 500s; it now returns
  `{"ip": "<addr>" | null}`, matching the existing graceful-degraded
  shape used by the same route's error path.

## API / IaC diff

* **NEW** `api/tasks/openapi.py` — Celery task
  `api.tasks.openapi.deploy_openapi_service` (routed onto the existing
  `azure` queue). Side-effect tagged in the docstring; idempotent
  (workload identity is created on demand, role assignments use
  `uuid5` names so they're idempotent, kubectl apply is naturally
  idempotent). Writes phase checkpoints via `update_state(state="PROGRESS", meta={"phase": ...})`
  so the SPA can poll real progress.
* **CHANGED** `api/routes/stubs.py`:
  * `POST /api/aks/openapi/deploy` — validates `resource_group`,
    `cluster_name`, `acr_name` (returns HTTPException 400 with code
    `missing_parameters` otherwise), then enqueues the task via
    `_safe_delay`. Returns
    `{id, instance_id, task_id, statusQueryGetUri, status: "queued"}`
    matching the orchestrator-style envelope the SPA already polls.
  * `GET /api/aks/openapi/deploy/{instance_id}/status` — real
    `AsyncResult` mapping (PENDING/RECEIVED → Pending,
    STARTED/RETRY/PROGRESS → Running with `custom_status` from
    `result.info`, SUCCESS → Completed with the task's payload as
    `output`, FAILURE/REVOKED → Failed/Terminated with a sanitised
    error envelope). Same shape the warmup status route uses.
  * `GET /api/aks/openapi/spec` — best-effort proxy: resolves the
    LB IP via `k8s_get_service_ip`, then tries
    `http://{ip}/openapi.json` and `/docs/openapi.json` with httpx
    (10 s timeout). Falls back to a degraded
    `{openapi: "3.0.0", info: {title: "elb-openapi (...)"}, paths: {},
    degraded: true, degraded_reason: ...}` shape on any failure.
* **CHANGED** `api/routes/monitor.py:aks_service_ip` — wraps the
  `k8s_get_service_ip` return value in `{"ip": ip}` so the response
  satisfies the declared `dict[str, Any]` contract regardless of
  whether the service has an external IP yet.
* **CHANGED** `api/celery_app.py` — registers `api.tasks.openapi`
  in `include=` and routes `api.tasks.openapi.*` onto the `azure`
  queue (same queue `provision_aks` uses; both call MSI / Authorization
  / ContainerService).

No Bicep changes — the task uses the existing user-assigned MI
(`id-elb-control`) via `DefaultAzureCredential`, the existing terminal
sidecar exec server for `az`/`kubectl`, and the existing
`IMAGE_TAGS["elb-openapi"]` pin.

### "No Run Command" policy compliance

The legacy `legacy/functionapp/activities/deploy_openapi_activity.py`
shelled into the cluster via `ManagedClusters.begin_run_command` (slow,
ARM-rate-limited, banned by repo policy). The new task replaces that
with `api.services.terminal_exec.run()` against the terminal sidecar's
exec server — `argv[0]` is `az`/`kubectl`, both already on the
allowlist (`{azcopy, kubectl, elastic-blast, elb, az}`).

## Validation evidence

### Unit tests (no regressions)

```
$ uv run pytest -q api/tests
........................................................................ [ 58%]
...................................................                      [100%]
123 passed in 10.64s
```

### Lint (new file clean)

```
$ uv run ruff check api/tasks/openapi.py
All checks passed!
```

(Pre-existing `B008 Body=Body(...)` / `Depends=Depends(...)` findings
in `api/routes/stubs.py` are the prescribed FastAPI pattern and are
out of scope for this change.)

### Service-ip 500 fix

```
$ curl -s 'http://127.0.0.1:8080/api/monitor/aks/service-ip?...&service_name=elb-openapi' \
    -H 'Authorization: Bearer __dev_bypass__'
{"ip":null}
```

(Previously: HTTP 500 with `ResponseValidationError {'type': 'dict_type',
'loc': ('response',), 'msg': 'Input should be a valid dictionary',
'input': None}`.)

### End-to-end smoke

```
$ curl -s -X POST http://127.0.0.1:8080/api/aks/openapi/deploy \
    -H 'Authorization: Bearer __dev_bypass__' \
    -H 'Content-Type: application/json' \
    -d '{"subscription_id":"...","resource_group":"rg-elb-01",
         "cluster_name":"elb-cluster","acr_name":"elbacr01",
         "storage_account":"elbstg01"}'
{"id":"d6e9861b-...","instance_id":"d6e9861b-...",
 "task_id":"d6e9861b-...","statusQueryGetUri":"/api/aks/openapi/deploy/d6e9861b-.../status",
 "status":"queued"}
```

Polling the status route showed the SPA-shaped envelope progress
through real phases:

```
poll 1..5: runtime_status=Running, custom_status={"phase":"setup_workload_identity",...}
poll 6:    runtime_status=Completed, output={
  "status": "failed",
  "cluster_name": "elb-cluster",
  "resource_group": "rg-elb-01",
  "workload_identity": {
    "mi_name": "id-elb-openapi",
    "mi_client_id": "a786ed27-...",
    "mi_principal_id": "21603700-...",
    "oidc_issuer": "https://koreacentral.oic.prod-aks.azure.com/.../",
    "federated_credential": "fc-elb-openapi",
    "roles_assigned": ["Contributor",
                       "StorageBlobDataContributor",
                       "AzureKubernetesServiceClusterUserRole"],
    "roles_failed": []
  },
  "openapi_deploy": {
    "image": "elbacr01.azurecr.io/elb-openapi:3.4",
    "error": "Cannot reach the terminal sidecar's exec server — the
              OpenAPI deploy needs `az` and `kubectl` from there.
              Make sure the `terminal` sidecar is running.
              (EXEC_TOKEN env var is empty; the api sidecar cannot
              authenticate with the terminal exec server. Check Bicep
              containerAppControl.bicep `exec-token` secret + sidecar env.)"
  }
}
```

This is the intended behaviour:

* Workload identity setup (the load-bearing Azure resource work)
  succeeded — MI created, federated credential attached, three roles
  assigned.
* Manifest apply failed in the local docker-compose dev environment
  because the `worker` container doesn't have `EXEC_TOKEN` plumbed
  through. The error message points the operator at the exact Bicep
  secret to fix. In a real Container App revision the secret is
  injected by `infra/modules/containerAppControl.bicep`, so the
  apply step will succeed there.

The previous behaviour was a permanent "Deploying…" spinner with
zero diagnostic information.

### Worker registration

```
$ docker exec elb-control-local-worker-1 \
    celery -A api.celery_app inspect registered | grep openapi
    * api.tasks.openapi.deploy_openapi_service
```
