# 2026-05-15 — elb-openapi pod scheduling on system/user pool split clusters

## Motivation

After the AKS provisioning task adopted the sibling repo's two-pool
layout (`systempool` with `CriticalAddonsOnly=true:NoSchedule`,
`blastpool` with `workload=blast:NoSchedule`, see
`2026-05-15-aks-system-user-pool-split.md`), the `elb-openapi`
deployment created by `api/tasks/openapi.py` had **no tolerations and no
nodeSelector**. On every newly provisioned cluster the pod sat in
`Pending` forever:

```
Warning  FailedScheduling  …  default-scheduler  0/4 nodes are available:
  4 node(s) had untolerated taint(s).
```

The Celery deploy task itself reported `succeeded` (`kubectl apply`
created the resources, the LoadBalancer got a public IP) but
`/openapi.json` was unreachable, so the dashboard's `/docs` page showed
*"Failed to load openapi.json"* / *"elb-openapi (spec not available)"*
with **0 endpoints**.

Sibling [`elastic-blast-azure/src/elastic_blast/constants.py`](https://github.com/dotnetpower/elastic-blast-azure)
documents the design intent:

```python
# system pool runs only AKS add-ons (CoreDNS, metrics-server, …);
# blast pool runs every ElasticBLAST workload pod.
ELB_AZURE_BLAST_NODE_TAINT = 'workload=blast:NoSchedule'
ELB_AZURE_SYSTEM_POOL_TAINT = 'CriticalAddonsOnly=true:NoSchedule'
```

`elb-openapi` is part of the BLAST control surface (REST API for the
ElasticBLAST CLI), not an AKS add-on, so it belongs on the blast pool.

## User-facing change

* `/docs` now renders the live OpenAPI spec on freshly provisioned
  clusters. Before the fix, the deploy reported "succeeded" but the
  swagger view never populated because the pod could not be scheduled.
* No UI changes — the existing **Deploy** card (first install) and the
  **Update OpenAPI service** card (re-roll) both work end-to-end now.

## API / IaC diff summary

`api/tasks/openapi.py` (`deploy_manifest.spec.template.spec`):

* Added a single `workload=blast:NoSchedule` toleration matching
  `ELB_AZURE_BLAST_NODE_TAINT` from the sibling repo.
* Added `nodeSelector: {workload: blast}` matching
  `ELB_AZURE_BLAST_NODE_LABEL_KEY`/`VALUE`. This pins the pod to the
  blast pool deterministically rather than relying on the system pool
  being temporarily uncordoned.

No other files touched. Frontend, infra, RBAC, and the `Update` button
(2026-05-15-openapi-update-button.md) are unchanged — this is a one-line
manifest hardening inside the existing Celery task.

## Validation evidence

```text
# Before
$ kubectl get pods -l app=elb-openapi
NAME                          READY   STATUS    RESTARTS   AGE
elb-openapi-7468cb776-dk4jk   0/1     Pending   0          14m

# After re-deploy
$ kubectl get pods -l app=elb-openapi -o wide
NAME                           READY   STATUS    RESTARTS   AGE   NODE
elb-openapi-764f75c994-2fbbp   1/1     Running   0          61s   aks-blastpool-29243661-vmss000001

$ kubectl get deployment elb-openapi -o jsonpath='{.spec.template.spec.tolerations}'
[{"effect":"NoSchedule","key":"workload","operator":"Equal","value":"blast"}]

$ curl -s -o /dev/null -w "%{http_code} %{size_download}\n" http://20.249.147.217/openapi.json
200 9706

$ curl -s http://20.249.147.217/openapi.json | jq '.info.title, .paths | length'
"ElasticBLAST on Azure"
8
```

Browser screenshot of `/docs` on `127.0.0.1:8090` confirms the spec
renders: **9 Endpoints · 3 Groups · GET/POST/DELETE methods**, with the
Swagger UI link pointing at `http://20.249.147.217` and the System group
listing `GET /healthz`, `GET /v1/health`, `GET /v1/config`, etc.

`uv run pytest -q api/tests` — **136 passed**.

## Operational note

If the AKS kubelet identity does not yet hold `AcrPull` on the registry
that hosts `elb-openapi:<tag>`, the new pod will land on a blast node
but stay in `ImagePullBackOff` with
`401 Unauthorized` from
`https://<acr>.azurecr.io/oauth2/token`. Recovery (one-shot, idempotent):

```bash
KUBELET_OBJ=$(az aks show -g <rg> -n <cluster> \
  --query "identityProfile.kubeletidentity.objectId" -o tsv)
ACR_ID=$(az acr show -n <acr> -g <acr-rg> --query id -o tsv)
az role assignment create --assignee-object-id "$KUBELET_OBJ" \
  --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope "$ACR_ID"
kubectl delete pod -l app=elb-openapi   # forces fresh pull
```

This is the same recovery hint already documented in
`docs/auth.md` §1; the deploy task does not currently grant AcrPull
itself because the dashboard MI typically lacks `User Access
Administrator`. Out of scope for this fix.
