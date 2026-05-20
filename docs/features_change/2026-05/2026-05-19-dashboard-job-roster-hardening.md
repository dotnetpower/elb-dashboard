# Dashboard job roster hardening

## Motivation

The AKS dashboard cluster pulse could make the workspace look worse than the live execution plane. A completed OpenAPI job could stay visible as `RUNNING 0/?`, active counts could be derived from stale dashboard state, and long DB URLs could crowd the roster. The root issue was that the dashboard consumed `/api/blast/jobs` list rows without enough cluster context or per-job OpenAPI detail enrichment.

## User-facing change

The dashboard job roster now scopes polling to the selected workspace and cluster, shows external OpenAPI execution shard counts when available, uses shorter database and query labels, and downgrades stale active rows with no execution signal so they do not inflate the active job count.

## API / UI diff summary

- `/api/blast/jobs` enriches external OpenAPI list rows with per-job detail for the first bounded set of rows shown on the dashboard.
- `/api/blast/jobs` now applies `subscription_id`, `resource_group`, and `cluster_name` to local Table-backed dashboard jobs as well as external OpenAPI jobs, so scoped requests cannot leak rows from another cluster.
- External OpenAPI job rows expose `splits_total`, `splits_done`, `splits_failed`, and `query_label` in the canonical dashboard job shape.
- External DB URLs are collapsed to their final database label before reaching dashboard cards.
- `ClusterPulse` now calls `blastApi.listJobs` with `subscriptionId`, `resourceGroup`, and `clusterName` so OpenAPI discovery and fallback are workspace-scoped.
- The dashboard BLAST Jobs card, Jobs menu page, latest-job chip, and alternate AKS bento card now share the same scoped jobs source instead of mixing unscoped and cluster-scoped list calls.
- Local `scripts/dev/local-run.sh web` now defaults `VITE_AUTH_DEV_BYPASS=true` and `VITE_API_BASE_URL=http://localhost:8085`, matching the local API's dev-auth defaults and avoiding localhost Jobs 401s caused by mismatched auth mode.
- `quick-deploy.sh` and `postprovision.sh` now share an ACR build-access policy helper: open the deployment registry for ACR Tasks, verify the desired network policy, allow propagation, then restore the previous network posture automatically.
- The Jobs menu honors the `?cluster=` deep link emitted by AKS job previews.
- `toJobRowView` reads external `output.execution` / `payload.external.execution` and maps it to progress instead of showing `0/?`.
- Jobs surface counts and filters use the same `toJobRowView`-backed classifier instead of duplicating local terminal/failed phase arrays.
- Stale active rows without progress for more than 30 minutes become `Unknown` with an explicit stale-state note.
- Unknown split counts render a zero-width progress bar rather than a fake minimum progress sliver.

## Validation evidence

- Added backend regression coverage for external list row enrichment: stale `running` list row plus `success` per-job detail now returns `status=completed`, `db=16S_ribosomal_RNA`, `query_label=query.fa`, and `splits_total/splits_done=1`.
- Added frontend regression coverage for external execution mapping and stale active row downgrading.
- Live AKS check before the change showed no active `blast|submit|finalizer` Kubernetes Jobs while the dashboard still displayed an active running row, confirming the display path needed hardening.
