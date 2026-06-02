---
title: elb-openapi ServiceAccount bound to cluster-admin so elastic-blast submit works
description: Replace the narrow elb-openapi-role ClusterRole with a direct cluster-admin binding so elastic-blast submit can apply its janitor RBAC, create-workspace DaemonSet, and PersistentVolumes — fixing BLAST jobs that died as "submit produced no BLAST jobs before stuck timeout".
tags:
  - blast
  - infra
  - security
---

# elb-openapi ServiceAccount → cluster-admin (BLAST submit RBAC fix)

## Motivation

BLAST jobs submitted through the OpenAPI execution plane (`POST /v1/jobs`,
served by the `elb-openapi` pods on AKS) failed at runtime. The dashboard
showed a `FAILED blastn core_nt` badge, and the job status carried:

```
submit produced no BLAST jobs before stuck timeout
```

### Root cause (confirmed via live pod logs)

`elastic-blast submit` runs inside the `elb-openapi` pod as the
`elb-openapi-sa` ServiceAccount. The pod's `/app/elastic-blast.log` showed:

```
kubectl --context=incluster apply -f .../templates/elb-janitor-rbac.yaml
Error from server (Forbidden): clusterrolebindings.rbac.authorization.k8s.io
"elb-janitor-rbac" is forbidden: User "system:serviceaccount:default:elb-openapi-sa"
cannot get resource "clusterrolebindings" in API group "rbac.authorization.k8s.io"
at the cluster scope
```

`elastic-blast submit` applies a broad set of cluster-scoped objects on every
submit:

- a janitor `ClusterRoleBinding` (`elb-janitor-rbac.yaml`) that binds the
  default ServiceAccount to the built-in `cluster-admin` ClusterRole,
- a `create-workspace` DaemonSet in `kube-system`,
- PersistentVolumes + a StorageClass,
- the per-batch BLAST Jobs.

The shipped `elb-openapi-role` ClusterRole only granted
`nodes/pods/configmaps/services` (core), `batch/jobs`, and `apps/deployments`
(read-only). Every openapi-driven `core_nt` submit therefore marched through a
cascade of 403s — `clusterrolebindings` forbidden → `serviceaccounts`
forbidden → `daemonsets` forbidden — and never created any BLAST Jobs, so the
watchdog marked the job failed. Only terminal/CLI submissions (which carry the
cluster-admin kubeconfig) had ever succeeded.

Scoping below `cluster-admin` is also security theater here: to apply the
janitor binding the SA must hold `bind`/`escalate` on the `cluster-admin`
ClusterRole, which already lets it grant itself cluster-admin at will. The
`elb-openapi` pod is internal-only (private LoadBalancer, no public ingress)
and is the trusted BLAST control plane that runs `elastic-blast submit`, so a
direct `cluster-admin` binding is both the honest representation of its
privilege level and the only configuration that keeps pace with
elastic-blast's evolving manifest set without whack-a-mole RBAC patches.

## User-facing change

OpenAPI-driven BLAST submits against partitioned databases (e.g. `core_nt`)
now run to completion instead of failing at submit. No UI change.

## API / IaC diff summary

- `api/tasks/openapi/manifests.py` `build_manifests`: removed the narrow custom
  `elb-openapi-role` ClusterRole (and dropped it from the manifest document
  list); repointed the `elb-openapi-binding` ClusterRoleBinding `roleRef` to
  the built-in `cluster-admin` ClusterRole.
- `api/tests/test_openapi_task.py`: replaced the (interim) custom-role
  assertion with `test_build_manifests_grants_janitor_rbac_permissions`, which
  asserts the binding targets `cluster-admin`, binds `elb-openapi-sa`, and that
  the redundant `elb-openapi-role` ClusterRole is no longer emitted.

## Live remediation (applied to `elb-cluster-02`)

The running cluster was unblocked immediately without a full redeploy:

```bash
kubectl delete clusterrolebinding elb-openapi-binding   # roleRef is immutable
kubectl apply -f -   # recreated binding → cluster-admin
kubectl delete clusterrole elb-openapi-role             # orphaned by the change
```

The next `api.tasks.openapi.deploy.deploy_openapi_service` run emits the same
`cluster-admin` binding, so the live state and the manifest builder agree.

## Validation evidence

- The previously-failing janitor apply now succeeds from inside the pod:
  `kubectl --context=incluster apply -f .../elb-janitor-rbac.yaml` →
  `clusterrolebinding.rbac.authorization.k8s.io/elb-janitor-rbac unchanged`.
- End-to-end submit of a `blastn` / `core_nt` job through the pod's
  `POST /v1/jobs` transitioned `dispatching → submitting → running →
  **completed**` (job `092eb3103fdb`), confirming BLAST Jobs are created and
  finish.
- `uv run pytest -q api/tests/test_openapi_task.py
  api/tests/test_openapi_deploy_contract.py api/tests/test_smoke.py` → 98
  passed.
- `uv run ruff check api/tasks/openapi/manifests.py
  api/tests/test_openapi_task.py` → clean.

## Out of scope (sibling repo)

A separate `docker-openapi` (sibling `elastic-blast-azure`) default surfaced
once RBAC was fixed: Mode B submits to a partitioned DB without an explicit
`blast_options.outfmt` fall back to `-outfmt 7`, which Partitioned BLAST
rejects at merge ("7 is not supported for merge"). The dashboard's own UI
submit already sends `outfmt 5`, so the UI flow is unaffected; the raw OpenAPI
Model B example that omits `outfmt` should set `outfmt` to `5` or `6`. Fixing
the sibling default requires rebuilding the `elb-openapi` image and is tracked
separately.
