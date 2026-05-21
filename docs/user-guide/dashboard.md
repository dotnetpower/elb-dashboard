# Dashboard

The Dashboard is the operator landing page for the ElasticBLAST control plane. It summarizes platform readiness across AKS, Storage, ACR, sidecars, terminal access, and recent BLAST activity.

## Overview

![Dashboard overview desktop](../images/screenshots/dashboard-overview-desktop.png)

Use the Dashboard to check whether the workspace is ready for BLAST work before opening a new search. The top controls select the active subscription and workload resource group, and the page groups readiness by operational plane:

- Cluster Plane shows AKS cluster readiness and node pool signals.
- Resource Plane shows ACR images, Storage network posture, BLAST database readiness, and terminal availability.
- Sidecar Runtime shows the local Container Apps sidecar flow used by the control plane.
- BLAST Jobs shows current search activity and recent job counts.

Cards can show healthy, degraded, loading, or unavailable states. A degraded state usually means the dashboard can still render but the backing Azure resource, sidecar, or API call needs attention before a workflow should continue.

## Cluster Plane

Use **Cluster Plane** to create or inspect the AKS workload cluster used by ElasticBLAST jobs. When no suitable cluster exists, click **Add Cluster** and choose the workload node size and node count for the databases researchers plan to use. The create dialog shows both the BLAST workload pool and the smaller system pool, plus the estimated hourly compute cost.

![Create AKS cluster dialog with workload pool, system pool, region, resource group, and estimated cost](../images/screenshots/create-aks-cluster.png)

For a first smoke test, prefer a small database and modest cluster. For larger databases such as `core_nt`, `nt`, or `nr`, choose enough node memory for the prepared shard layout and warmup target.

## BLAST Databases

Use the **BLAST Databases** control to prepare databases before New Search. Click the database icon from the Dashboard resource plane, then click **Get** next to the NCBI database to copy it into the workload Storage account. Prepared databases show downloaded size, file count, version time, shard layout availability, readiness, and optional auto-warm status.

![BLAST Databases dialog showing available NCBI databases, Get actions, prepared Core nucleotide shards, and Auto warm](../images/screenshots/get-database.png)

The database step is more than a download. The prepare flow records metadata and creates shard layouts so submit can choose an execution path that fits the selected AKS cluster. Enable auto warm for the database you expect researchers to use often, then verify warmup status before submitting the first search.

## Mobile Layout

![Dashboard mobile layout](../images/screenshots/dashboard-mobile.png)

On narrow screens, the same cards stack vertically so the readiness flow stays readable. Use the navigation menu at the top left to move between Dashboard, New Search, Recent searches, Custom DB, Lab Tools, Terminal, and API views.

## Screenshot Targets

Screenshots for this page are defined by these manifest targets:

- `dashboard-overview-desktop`
- `dashboard-mobile`
- `create-aks-cluster`
- `get-database`

Refresh these images after visible dashboard layout changes, navigation changes, or readiness-state presentation changes.