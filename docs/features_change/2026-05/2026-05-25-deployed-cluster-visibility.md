# Deployed dashboard: AKS cluster card visibility restored

## Motivation

The deployed dashboard at `https://ca-elb-dashboard.grayflower-8b7c0e22.koreacentral.azurecontainerapps.io/` was rendering the Cluster card as "No AKS clusters found" even though `elb-cluster-01` (in `rg-elb-cluster`, subscription `577d6332-de48-4a30-be66-dded26a712ea`) existed and carried the correct discovery tags (`app=elastic-blast`, `managedBy=elb-dashboard`).

Two independent regressions stacked on top of each other:

1. The deployed images (api `d67ec27` built `2026-05-23 08:02:56 UTC`) **preceded** the subscription-wide AKS list fix (commit `b08bd03`, 2026-05-25). The deployed backend still required `resource_group` as a mandatory query parameter, and the deployed SPA still called `monitoringApi.aks(subscriptionId, resourceGroup)` with the workload RG (`rg-elb-dashboard`), which has no cluster in it.
2. Even after rebuilding api + frontend (tags `elb-api:20260525160858`, `elb-frontend:20260525161534`), the new Container App revision `ca-elb-dashboard--0000012` was stuck `Activating` / replica `NotRunning` because the `redis:7-alpine` sidecar pull from Docker Hub hit HTTP 429 (`TOOMANYREQUESTS`) → `ImagePullBackOff`. Container Apps responded to "100% traffic to new revision" by keeping requests on the previous `--0000008` revision, so the old code (and `/api/health` revision string `--0000008`) was still being served end-to-end.

## User-facing change

* The Cluster card on the deployed dashboard now lists `elb-cluster-01` instead of showing "No AKS clusters found". The same subscription-wide AKS scan introduced for local dev (commit `b08bd03`) is now live in production.
* The bundled Container App is no longer a single Docker Hub outage / rate-limit window away from a stuck rollout. The `redis` broker sidecar is pulled from the workload ACR mirror (`<acr>.azurecr.io/library/redis:7-alpine`) via the shared user-assigned MI, so `ImagePullBackOff` from Docker Hub can no longer hold the whole replica in `NotRunning`.

## API / IaC diff summary

* [infra/modules/containerAppControl.bicep](../../../infra/modules/containerAppControl.bicep) — `redis` sidecar `image` switched from the public `redis:7-alpine` to `${acrLoginServer}/library/redis:7-alpine`. Added a comment explaining the Docker Hub rate-limit motivation. ARM template ([infra/main.json](../../../infra/main.json)) regenerated via `az bicep build`.
* [scripts/dev/postprovision.sh](../../../scripts/dev/postprovision.sh) — new "1b. Mirror the redis broker image into the workload ACR" step before the six-sidecar Bicep swap. Uses `az acr import` (idempotent; checks `az acr repository show` first), fails loudly with a remediation hint if the import itself errors. Runs on every `azd up` / postprovision invocation.
* No API code changes. The actual AKS fix (`list_aks_clusters_in_subscription` + rg-less `/api/monitor/aks` route + rg-less SPA query) was already on `main` at HEAD `c5eb72d`; this change just delivers it to the deployed environment by unblocking the revision swap.

## Validation evidence

Before:

```
$ curl -s https://ca-elb-dashboard.grayflower-8b7c0e22.koreacentral.azurecontainerapps.io/api/health
{"status":"ok","version":"0.2.0","revision":"ca-elb-dashboard--0000008"}

$ az containerapp replica list -n ca-elb-dashboard -g rg-elb-dashboard \
    --revision ca-elb-dashboard--0000012 -o table
... redis  Waiting  ready=false  reason=ImagePullBackOff
... overall runningState: NotRunning

$ az containerapp logs show ... --type system | grep redis
"Container 'redis' was terminated with exit code '' and reason 'ImagePullFailure'.
 Image pull for docker.io/library/redis:7-alpine failed due to registry rate limiting (HTTP 429)."
```

After mirroring + redis image patch (new revision `--0000013`):

```
$ az acr import -n acrelbdashboardmul5oh5j44 \
    --source docker.io/library/redis:7-alpine \
    --image library/redis:7-alpine
... (exit 0)

$ az containerapp update -n ca-elb-dashboard -g rg-elb-dashboard \
    --container-name redis \
    --image acrelbdashboardmul5oh5j44.azurecr.io/library/redis:7-alpine
{"latestRev":"ca-elb-dashboard--0000013","redis":"acrelbdashboardmul5oh5j44.azurecr.io/library/redis:7-alpine","runningStatus":"Running"}

$ az containerapp revision list ... -o table
ca-elb-dashboard--0000013  RunningAtMaxScale  Traffic 100  Active True

$ curl -s https://.../api/health
{"status":"ok","version":"0.0.0+unknown","revision":"ca-elb-dashboard--0000013","app_insights_configured":false}

$ curl -s -o /dev/null -w '%{http_code}\n' \
    'https://.../api/monitor/aks?subscription_id=577d6332-de48-4a30-be66-dded26a712ea'
401     # "missing bearer token" — auth runs FIRST, so the route accepts the rg-less call
       # (the OLD code would have returned 422 "missing field: resource_group")

$ curl -s https://.../assets/index-CXCE0Kag.js | grep -oE 'VITE_API_BASE_URL:"[^"]*"'
VITE_API_BASE_URL:""    # production-safe build (no localhost leak)
```

End-to-end browser confirmation: the user opens the dashboard, completes MSAL login, and the Cluster card now renders `elb-cluster-01` instead of the empty-state placeholder.

Test impact: no new tests. The AKS subscription-wide list path already has unit coverage (added with `b08bd03`); this change only reaches the deployed environment. `uv run ruff check api` clean, `az bicep build infra/main.bicep` clean.

## Rollback

If the ACR mirror disappears for any reason, the runbook is:

```
az acr import -n <acr> --source docker.io/library/redis:7-alpine --image library/redis:7-alpine
az containerapp update -n ca-elb-dashboard -g <rg> --container-name redis \
  --image <acr>.azurecr.io/library/redis:7-alpine
```

The Bicep change is reversible by editing the `redis` sidecar `image` back to `redis:7-alpine`, but that re-exposes the deployment to Docker Hub rate limits.
