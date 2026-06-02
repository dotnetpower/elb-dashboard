---
title: elb-openapi in-cluster kubeconfig fixes /v1/ready 503
description: Inject an in-cluster kubeconfig into the elb-openapi Deployment so its kubectl reaches the API server, fixing the recurring /v1/ready 503 k8s_unreachable.
tags:
  - blast
  - infra
---

# elb-openapi in-cluster kubeconfig (`/v1/ready` 503 fix)

## Motivation

The `elb-openapi` service `/v1/ready` endpoint returned HTTP 503 with
`k8s_unreachable` even when the target AKS cluster (`elb-cluster-02`) was fully
`Running`/`Succeeded`. The error detail was:

```
The command "kubectl get --raw /readyz --request-timeout=1s" returned with exit code 1
The connection to the server localhost:8080 was refused - did you specify the right host or port?
```

### Root cause (confirmed via live pod inspection)

The `/v1/ready` probe shells out to `kubectl get --raw /readyz`. The **kubectl
CLI does not auto-load in-cluster configuration** the way the client-go
`InClusterConfig()` helper does. The pod had:

- the in-cluster env vars present (`KUBERNETES_SERVICE_HOST`, `KUBERNETES_SERVICE_PORT`),
- the projected ServiceAccount token mounted at
  `/var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt,namespace}`,
- valid `elb-openapi-sa` RBAC,

but **no `~/.kube/config` and an empty `KUBECONFIG`**, so kubectl fell back to
its default `localhost:8080` and every cluster call failed with
"connection refused".

Validated live that writing a `tokenFile`-based in-cluster kubeconfig and
pointing `KUBECONFIG` at it makes the exact probe command succeed:

```
kubectl get --raw /readyz      -> ok
kubectl get deploy elb-openapi -> 2 readyReplicas
```

## User-facing change

`/v1/ready` now returns 200 (cluster reachable) instead of 503
`k8s_unreachable` once the elb-openapi Deployment is re-applied with the new
manifest. No SPA code change required.

## API / IaC diff summary

`api/tasks/openapi/manifests.py` (`build_manifests`):

- **New `ConfigMap` `elb-openapi-kubeconfig`** holding an in-cluster kubeconfig.
  It uses `tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token`
  (auto-rotated), `certificate-authority: .../ca.crt`, and
  `server: https://kubernetes.default.svc` (the standard in-cluster API
  endpoint, always present in the API server certificate SAN).
- **New container env** `KUBECONFIG=/etc/elb/kube/config` (added to `openapi_env`).
- **New volume** `incluster-kubeconfig` (ConfigMap-backed) mounted read-only at
  `/etc/elb/kube` on the `openapi` container.
- ConfigMap appended to the multi-document output (between RBAC and Deployment).

The fix lives entirely in the dashboard-owned manifest — no sibling
`elastic-blast-azure` image change is needed. The default projected
ServiceAccount token mount is relied upon (`automountServiceAccountToken`
remains the default `true`).

## Validation evidence

- `uv run ruff check api/tasks/openapi/manifests.py` — clean.
- `uv run pytest -q api/tests/test_smoke.py api/tests/test_openapi_task.py` —
  91 passed. Existing tests look up documents by `kind`, so the additional
  ConfigMap document is non-breaking.
- Live proof that the kubeconfig approach works (IP-based variant):
  `kubectl get --raw /readyz` → `ok`, `kubectl get deploy elb-openapi` →
  `2 readyReplicas`.

## Rollout

This changes the `elb-openapi` Kubernetes manifest (not ordinary api/web code),
so a redeploy of the `api` sidecar followed by the dashboard
"Deploy elb-openapi" action (`POST /api/aks/openapi/deploy`) is the sanctioned
path to apply the new ConfigMap + volume and recreate the pods.
