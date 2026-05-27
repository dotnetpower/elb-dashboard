# Public HTTPS pipeline — systempool toleration injection + SPA dict-failure surfacing

## Motivation
On 2026-05-27 the user clicked **Settings → Public HTTPS → Enable** on
`elb-cluster-01`. The task ran for 387 s and the SPA showed no error, but the
endpoint was not active. Log Analytics for the `worker` sidecar showed:

```
public-https: cert-manager-webhook rollout probe N/5 not ready yet: error: timed out waiting for the condition
RuntimeError: kubectl rollout status cert-manager-webhook failed after 5 probes (360s elapsed)
Task ...setup_openapi_public_https... succeeded in 387.83s:
  {'status': 'failed',
   'error': 'kubectl rollout status cert-manager-webhook failed after 5 probes (360s elapsed); ...',
   'fqdn': 'elb-openapi-a4f11ec1f3.koreacentral.cloudapp.azure.com',
   'elapsed_seconds': 387}
```

Direct `kubectl describe` on the cluster:

```
FailedScheduling: 0/3 nodes are available: 3 node(s) had untolerated taint(s)
```

| Node | Taint |
|------|-------|
| `aks-systempool-...` | `CriticalAddonsOnly=true:NoSchedule` |
| `aks-blastpool-...-0` | `workload=blast:NoSchedule` |
| `aks-blastpool-...-1` | `workload=blast:NoSchedule` |

Both `cert-manager.yaml` and ingress-nginx's `deploy.yaml` ship pods with **no
tolerations**, so every workload Pod they created landed in `Pending` forever on
this AKS topology. The task happened to swallow the resulting `RuntimeError`
into a dict result, so Celery reported `runtime_status: Completed` and the SPA's
poll loop took that to mean "succeeded" → no banner, no toast, no error.

## User-facing change
* **Public HTTPS Enable now succeeds on AKS clusters with tainted node pools.**
  The setup task injects a `CriticalAddonsOnly` toleration and a
  `kubernetes.azure.com/mode=system` nodeSelector into every Deployment / Job
  from the upstream install manifests before applying, so cert-manager and
  ingress-nginx land on the systempool only (never on the BLAST workload pool).
* **The Settings panel now surfaces dict-level pipeline failures.** When the
  task returns `{status: 'failed', error: '...'}` despite Celery reporting
  `Completed`, the SPA flips to the error banner instead of silently
  switching the status badge to *Exposed*.

## API / IaC diff summary
* `api/services/k8s/ingress.py`
  * New constants `SYSTEM_POOL_TOLERATION`, `SYSTEM_POOL_NODE_SELECTOR`,
    `INGRESS_NGINX_ADMISSION_JOB_SELECTOR`.
  * New `patch_manifest_for_system_pool(raw_manifest) -> str` — pure transform
    that injects the systempool toleration + nodeSelector into every
    `Deployment | DaemonSet | StatefulSet | Job | ReplicaSet` podTemplate.
    Preserves existing tolerations / nodeSelector keys, is idempotent, and
    drops `None` separator docs so kubectl does not reject `--- null`.
  * New `fetch_install_manifest_for_system_pool(url, *, timeout_seconds=60)` —
    network wrapper around the pure transform.
* `api/tasks/openapi/public_https.py`
  * Step 1 (ingress-nginx install): now streams the patched manifest through
    `kubectl apply -f -` and pre-deletes any pre-existing admission-webhook
    Jobs (immutable spec from a prior toleration-less install).
  * Step 4 (cert-manager install): now streams the patched manifest through
    `kubectl apply -f -` (no Job pre-delete needed — cert-manager has no
    install-time Jobs).
* `web/src/components/SettingsPanel.tsx`
  * `pollTask` now treats `runtime_status === 'Completed' && output.status ===
    'failed'` as a failure path and surfaces `output.error` in the existing
    error banner.

No Bicep / Container App template / sidecar layout changes.

## Validation evidence
* `uv run pytest -q api/tests/test_openapi_public_https.py` → **22 passed**
  (4 new unit tests for `patch_manifest_for_system_pool` covering inject,
  preserve-existing, idempotency, and `--- null` separator regression;
  full pipeline test updated to assert stdin-streamed apply + admission-Job
  pre-delete ordering).
* `uv run pytest -q api/tests` → 1530 passed, 1 unrelated flaky
  (`test_terminal_exec.py::test_run_truncates_stdout_above_cap`, timing-
  dependent, passes when isolated).
* `uv run ruff check` on changed files → all checks passed.
* `cd web && npx tsc --noEmit` → no errors.
* `cd web && npm run build` → ok.
* **Live verification on elb-cluster-01** (manual `kubectl apply -f -` of
  the patched manifests):
  ```
  cert-manager-b5474d6b5-8xj4q               1/1     Running   aks-systempool-...
  cert-manager-cainjector-6498996479-qzn2g   1/1     Running   aks-systempool-...
  cert-manager-webhook-7cf4687596-bllbw      1/1     Running   aks-systempool-...
  ingress-nginx-admission-create-h5xff       0/1     Completed aks-systempool-...
  ingress-nginx-admission-patch-w9jpw        0/1     Completed aks-systempool-...
  ingress-nginx-controller-549f67b9b5-4grnz  1/1     Running   aks-systempool-...
  ```
  All add-on pods land on the systempool; nothing leaks onto the blastpool;
  the ingress-nginx LB keeps its existing EXTERNAL-IP `20.249.192.56`.
