# Grafana dashboards

Importable Grafana dashboards for visualising an ElasticBLAST run on the
elb-dashboard AKS cluster.

> Pair this with [docs/user-guide/observability.md](../docs/user-guide/observability.md)
> — that page explains why Managed Grafana + Managed Prometheus is the
> right surface for cluster-internal BLAST telemetry, and when to keep
> it disabled to avoid ingestion cost.

## Dashboards

| File | Purpose |
|------|---------|
| [dashboards/elb-blast-execution.json](./dashboards/elb-blast-execution.json) | Real-time overview of a BLAST run — active jobs, pod lifecycle by role, per-pod CPU & memory, CPU throttling, working-set/RSS pressure, blastpool node CPU/memory/disk/network, top-N tables, and OOM event annotations. |

## How this template is designed

Most "off-the-shelf" Kubernetes Grafana dashboards lean on
`kube_pod_labels` / `kube_job_*` series from
**kube-state-metrics**. Azure Monitor managed Prometheus's **default**
scrape config does *not* include kube-state-metrics, so those panels
silently render *No data*.

This template only uses metrics that are scraped by default:

* **cAdvisor** — `container_cpu_usage_seconds_total`,
  `container_memory_working_set_bytes`, `container_memory_rss`,
  `container_cpu_cfs_throttled_periods_total`, `container_oom_events_total`,
  `container_fs_reads_bytes_total`, `container_fs_writes_bytes_total`.
* **node-exporter** — `node_cpu_seconds_total`,
  `node_memory_Mem*_bytes`, `node_disk_*_bytes_total`,
  `node_network_*_bytes_total`.
* **kubelet** — `kubelet_running_pods`.

BLAST workloads are identified by **container name**
(`blast | submit | finalizer | init-pv`) rather than by pod labels, and
the `elb_job_id` variable is extracted at query time from the pod name
(`elb-<role>-<8-hex>-...`) with `label_replace`. As a result the
dashboard works against either `default` or `elastic-blast-*` namespaces
— the `namespace` template variable auto-discovers whichever namespace
the BLAST pods are actually running in.

## Prerequisites

The dashboard assumes the AKS cluster has the standard Azure Monitor
managed Prometheus stack enabled (the same stack that the `View
Grafana` button on the AKS Monitor blade configures):

* [Azure Monitor managed service for Prometheus](https://learn.microsoft.com/azure/azure-monitor/essentials/prometheus-metrics-overview)
  with the default scrape set (cAdvisor + node-exporter + kubelet).
* [Azure Managed Grafana](https://learn.microsoft.com/azure/managed-grafana/overview)
  workspace linked to the Azure Monitor workspace above.

If you provisioned the cluster through this repo, both are off by
default to keep the ingestion bill flat. Turn them on from
**AKS → Monitor → Monitor Settings** in the Azure Portal, wait a few
minutes for the metrics pipeline to come up, then import the dashboard.

You do **not** need kube-state-metrics. If you have it enabled it
won't hurt, but the template never reads it.

## Import

### From this repo via Azure CLI (recommended)

```bash
az grafana dashboard create \
  --name <your-grafana-workspace> \
  --resource-group <rg-of-your-grafana-workspace> \
  --definition @grafana/dashboards/elb-blast-execution.json \
  --overwrite true
```

`--overwrite true` updates the existing dashboard in place (preserving
the same UID `elb-blast-execution`) whenever you re-pull this repo.

### From the Grafana UI

1. Open your Managed Grafana workspace.
2. **Dashboards → New → Import**.
3. Upload `dashboards/elb-blast-execution.json` (or paste the JSON).
4. On first open, set the **Prometheus data source** template variable
   to your Managed Prometheus data source (usually named
   *Managed_Prometheus_<workspace>*). The default Azure Monitor data
   source is *not* Prometheus, so the panels show *No data* until this
   variable is switched.
5. Save.

## Variables

| Variable | Default | What it filters |
|----------|---------|-----------------|
| `DS_PROMETHEUS` | first Prometheus data source | All panels — pick *Managed_Prometheus_<workspace>* on first open. |
| `namespace` | `All` | All BLAST pod / container queries. Auto-populated from labels that have a BLAST container running. |
| `job_id` | `All` | Per-job filter. Extracted from pod name with `label_replace(..., "pod", "elb-[a-z-]+-([0-9a-f]{8})-.+")`. |

## Customising

* The dashboard uses `$__rate_interval` so `rate()` adapts to your
  scrape interval — no need to tune step.
* All cAdvisor queries filter
  `container=~"blast|submit|finalizer|init-pv"` to exclude pause / CNI
  / app sidecars (e.g. the `openapi` control-plane container that
  shares the `default` namespace). Add other container names if you
  introduce sidecars in ElasticBLAST.
* Node panels filter `instance=~"aks-blastpool.*"` to limit results to
  the `blastpool` nodepool. If you rename the nodepool, update that
  regex in panels 13 – 17.
* Annotations: an `OOM events` annotation marker is included by
  default. Disable it from **Dashboard settings → Annotations** if you
  don't want the red ticks on the timelines.

## Out of scope

* App Insights / control-plane sidecar telemetry — that lives in the
  Application Insights resource, not in Managed Prometheus. See
  [docs/user-guide/observability.md](../docs/user-guide/observability.md)
  for the App Insights queries.
* Pod logs — use Container insights (Log Analytics) directly, or add a
  Logs panel with the Azure Monitor data source filtered to
  `ContainerLogV2 | where PodName startswith "elb-"`.
* Pod-phase / job-completion panels — those rely on kube-state-metrics
  series (`kube_pod_status_phase`, `kube_job_*`). Re-enable them only
  after explicitly opting kube-state-metrics into the
  `ama-metrics-prometheus-config` ConfigMap.
