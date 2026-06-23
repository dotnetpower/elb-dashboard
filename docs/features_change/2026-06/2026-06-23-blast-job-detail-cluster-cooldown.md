---
title: Faster job detail under cluster outage + correct worker Sizing row
description: Cluster-level negative cache stops repeated ~27s K8s refresh timeouts on /api/blast/jobs/{id}, and the Sizing card now shows the worker sidecar's real 1.0/2.0Gi allocation.
tags:
  - blast
  - operate
---

# Faster job detail under cluster outage + correct worker Sizing row

Follow-ups to the api-sidecar resource diagnosis
([2026-06-23-api-sidecar-resource-bump](2026-06-23-api-sidecar-resource-bump.md)).

## Motivation

While diagnosing the api sidecar CPU bursts, Log Analytics showed
`/api/blast/jobs/{id}` taking **up to 27.5s**. Root cause: for a `running`
BLAST job the detail route calls `_refresh_running_blast_state`, which hits the
cluster's Kubernetes API via three serial GETs (`namespace`, `pods`, `jobs`)
each at `timeout=10`. When the cluster API is unreachable (auto-stopped,
API-server TLS handshake failing, or a network blip — the same condition that
made `aks_top_nodes` degrade with `SSLError: Max retries exceeded`),
`k8s_check_blast_status` swallows the timeout and returns `status="unknown"`
after blocking ~20-30s. The existing throttle (`_K8S_REFRESH_LAST_CHECK`) is
keyed per-job, so opening several running jobs' detail views each re-paid the
full timeout.

Separately, the Settings → Sizing card hard-coded the worker sidecar at
`0.5 vCPU / 1.0Gi` while the live deployment runs it at `1.0 / 2.0Gi`, so the
worker row under-reported its allocation and over-stated its utilization.

## User-facing change

- Opening BLAST job detail pages while the cluster API is unreachable no longer
  stalls ~27s per job. The first refresh still pays one timeout, but every
  sibling job on the same cluster then short-circuits for a cooldown window
  (default 60s, env `BLAST_K8S_REFRESH_FAILURE_COOLDOWN_SECONDS`). A reachable
  refresh clears the cooldown immediately, so recovery is not delayed.
- The Sizing card's `worker` row now shows the correct `1.0 vCPU / 2.0Gi`
  allocation and a correctly normalized utilization.

## API / IaC diff summary

- [api/services/blast/job_state.py](../../../api/services/blast/job_state.py):
  added `_K8S_REFRESH_CLUSTER_COOLDOWN` (a `(subscription, resource_group,
  cluster)` → cooldown-deadline map) and `_K8S_REFRESH_FAILURE_COOLDOWN_SECONDS`.
  `_refresh_running_blast_state` now skips the K8s call when the cluster is in
  cooldown, arms the cooldown on an exception **or** a `status="unknown"`
  result, and clears it on any reachable concrete status.
- [web/src/components/settings/sections/SizingSection.tsx](../../../web/src/components/settings/sections/SizingSection.tsx):
  `SIDECAR_RESOURCES.worker` `{cpu:0.5, memoryGi:1.0} → {cpu:1.0, memoryGi:2.0}`.

No contract/signature changes; the cooldown is additive and degrades to the
previous behavior when the cluster is reachable.

## Validation evidence

- New regression tests in
  [api/tests/test_local_to_blast_job.py](../../../api/tests/test_local_to_blast_job.py):
  `test_refresh_running_blast_state_cluster_cooldown_skips_sibling_jobs` and
  `test_refresh_running_blast_state_reachable_clears_cluster_cooldown`.
- `uv run pytest -q api/tests/test_local_to_blast_job.py -k refresh_running_blast_state`
  → 12 passed.
- `uv run pytest -q api/tests/test_local_to_blast_job.py
  api/tests/test_external_blast_api.py api/tests/test_blast_jobs_routes.py`
  → 190 passed.
- `uv run ruff check api/services/blast/job_state.py …` → clean.
- `cd web && npm run build` → built clean.

## Follow-up (not in this change)

- Code-level: `_fetch_blast_pods_and_jobs` issues three serial 10s-timeout GETs;
  a shorter connect timeout or a fail-fast on the first GET would shrink the
  one unavoidable first-hit timeout below ~27s. Deferred — the negative cache
  removes the repeated cost, which was the user-visible problem.

## Update (critique-hardening pass, 2026-06-24)

A 30-round critique-hardening pass over the SB queue / results / paths /
download conditions surfaced two minor findings on this cooldown:

- **Observability (R29)**: arming the cooldown was silent. Added
  `_arm_cluster_refresh_cooldown`, which logs one INFO line per outage episode
  (deduped across sibling jobs — re-arming an already-cooling cluster does not
  re-log) so a job-detail K8s outage is visible, not just the monitor card's
  `aks_top_nodes` degrade.
- **Map hygiene (R4)**: the cooldown check now pops a lapsed key instead of
  leaving an expired entry in `_K8S_REFRESH_CLUSTER_COOLDOWN`, so the map never
  retains stale keys and a re-failure logs as a fresh episode.

Regression: `test_refresh_running_blast_state_cooldown_logs_once_per_episode`
plus an autouse isolation fixture clearing the throttle + cooldown maps around
every refresh test. `uv run pytest -q api/tests/test_local_to_blast_job.py
api/tests/test_external_blast_api.py api/tests/test_blast_jobs_routes.py` → 191
passed.
