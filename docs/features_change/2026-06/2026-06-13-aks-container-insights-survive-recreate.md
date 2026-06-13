# AKS Container Insights survives cluster recreate (opt-in)

## Motivation

A user reported that the "App Insights connection" appeared to break after
deleting and recreating their AKS cluster. Investigation showed two distinct
surfaces were being conflated:

- **Control-plane Application Insights** (api / worker / beat → `appi-elb-dashboard`)
  was **healthy** — it lives on the Container App, not the cluster, and
  telemetry kept flowing (verified via the `log-elb-dashboard` Log Analytics
  workspace: hundreds of `AppRequests` / `AppTraces` on the day of the report).
  The `DEPLOYMENT · IDLE` badge in Settings → Telemetry is just the browser
  telemetry toggle being off, not a broken connection.
- **AKS Container Insights** (the `omsagent` addon) genuinely **does not survive
  a cluster delete + recreate**. The addon lives on the cluster resource, so a
  recreated cluster comes up with cluster observability off. Neither the
  dashboard's provision task nor `elastic-blast submit` re-enabled it, so all
  live ElasticBLAST clusters were observed with `omsagent` disabled.

## User-facing change

- The AKS provision task (`api.tasks.azure.provision_aks`) can now re-enable
  Container Insights on a freshly provisioned/recreated cluster, **off by
  default**. Operators opt in by setting
  `AKS_PROVISION_ENABLE_CONTAINER_INSIGHTS=true` on the api / worker sidecars.
  When enabled, the task enqueues the existing
  `api.tasks.azure.enable_aks_container_insights` task (which carries the
  LinkedAuthorizationFailed self-heal + workspace-RG RBAC retry) against the
  platform Log Analytics workspace.
- Default behaviour is unchanged: with the flag unset, provisioning never
  touches the `omsagent` addon, and clusters are re-enabled only via the manual
  **Settings → AKS Observability** flow (unchanged).
- Off-by-default is deliberate: Container Insights ships node/pod telemetry to
  the same `log-elb-dashboard` workspace the control plane uses (capped at
  1 GiB/day), so auto-enabling it on a busy BLAST cluster could exhaust the
  quota and starve the dashboard's own traces.

## API / IaC diff summary

- `api/tasks/azure/provision.py`: added `_container_insights_reenable_enabled()`
  and `_maybe_enqueue_container_insights()`; the provision task calls the latter
  (best-effort, never raises) after the cluster + RBAC are ready and returns the
  new optional `container_insights_task_id` field.
- `infra/modules/containerAppControl.bicep`: new `logAnalyticsWorkspaceResourceId`
  param; new `LOG_ANALYTICS_WORKSPACE_RESOURCE_ID` and
  `AKS_PROVISION_ENABLE_CONTAINER_INSIGHTS` (default `'false'`) env vars on the
  api and worker containers. Recompiled `containerAppControl.json` / `main.json`.
- `infra/main.bicep`: passes `monitoring.outputs.workspaceResourceId` into the
  control-app module.
- `scripts/dev/postprovision.sh`: resolves the workspace ARM resource id and
  passes it as `logAnalyticsWorkspaceResourceId` to the six-sidecar swap deploy.

## Validation evidence

- `uv run pytest -q api/tests/test_azure_provision_aks.py` — 34 passed
  (8 new cases covering the flag parser and the enqueue helper: default-OFF
  no-op, opted-in-but-no-workspace no-op, opted-in enqueue kwargs, and
  swallowed enqueue error).
- `az bicep build` for `infra/main.bicep` and
  `infra/modules/containerAppControl.bicep` — rc 0; new tokens present in the
  recompiled JSON.
- Live diagnosis: control-plane App Insights confirmed flowing via the
  `log-elb-dashboard` workspace; all ElasticBLAST clusters confirmed with
  `omsagent` disabled (the broken surface).
- Not redeployed — activation requires a deploy (to inject the workspace ARM id
  env) plus flipping the opt-in flag.
