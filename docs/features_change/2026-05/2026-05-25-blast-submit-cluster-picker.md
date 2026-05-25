# BLAST Submit — subscription-wide cluster picker

## Motivation

The dashboard's `ClusterCard` was migrated to a subscription-wide AKS list
(see [2026-05-25-aks-subscription-wide-list.md](./2026-05-25-aks-subscription-wide-list.md))
so researchers can operate the typical multi-cluster ELB fleet
(`heavy` / `light` / `gpu` / `general`, each in its own AKS-managed RG) from
a single screen. The Submit page was still pinned to the workspace
**anchor** RG (`config.workloadResourceGroup`), so:

* The cluster `<select>` only listed clusters in the anchor RG, even when
  the operator wanted to dispatch a job to a different tier in a different
  RG.
* The submit + preflight payloads hard-coded `resource_group: workloadRg`,
  which would have caused `list_aks_clusters(cred, sub, rg)` lookups to
  fail with "cluster not found in '<rg>'" once the picker was sub-wide.
* The empty-state copy ("No AKS clusters in <anchor-rg>") implied the
  anchor RG is the only valid scope — at odds with the Dashboard story.

## User-facing change

* Submit → **Compute** picker now lists every ELB-managed cluster the
  caller can see in the subscription (same filter as the ClusterCard).
* `<option>` label now reads
  `aks-elb-heavy — koreacentral · heavy · rg-blast-heavy (Running)` —
  region · tier · RG joined by middots, so multi-cluster fleets are
  unambiguous without expanding the row.
* Auto-selection still prefers the first **Running** cluster, falling
  back to the first available.
* Empty-state copy is now scope-honest:
  `No ELB-managed AKS clusters found in this subscription.`
* Preflight + submit payloads now carry the **selected cluster's** RG, so
  cross-RG dispatch works end-to-end without changing the backend
  contract.

## API / IaC diff

**Backend**: no change.
`/api/blast/pre-flight` and `/api/blast/submit` already accept any
`resource_group`; they only use it to locate the cluster via
`list_aks_clusters(cred, sub, rg)`. Sending the cluster's actual RG (which
the SPA now does) keeps the existing contract intact.

**Frontend**:

* [web/src/pages/blastSubmit/useClusterSelection.ts](../../../web/src/pages/blastSubmit/useClusterSelection.ts)
  — query swapped from RG-scoped `monitoringApi.aks(subId, workloadRg)` to
  sub-wide `monitoringApi.aks(subId)`; `workloadRg` field removed from the
  args interface (caller no longer passes it).
* [web/src/pages/blastSubmit/ComputeSection.tsx](../../../web/src/pages/blastSubmit/ComputeSection.tsx)
  — `<option>` label enriched with `tier` + `resource_group`; option `key`
  qualified by RG to remain unique when names collide across RGs (defensive,
  unlikely in practice); empty-state copy reworded.
* [web/src/pages/BlastSubmit.tsx](../../../web/src/pages/BlastSubmit.tsx)
  — `useClusterSelection` call drops the `workloadRg` arg; preflight
  payload uses `selectedCluster?.resource_group || workloadRg`;
  `handleSubmit` passes `selectedCluster.resource_group || workloadRg` as
  the `workloadRg` arg to `buildSubmitRequest` so the submit payload's
  `resource_group` follows the selected cluster.
* [web/src/pages/blastSubmit/taxonomyFilter.test.ts](../../../web/src/pages/blastSubmit/taxonomyFilter.test.ts)
  — added two regression tests (`emits the selected cluster's RG in the
  submit payload`, `uses the cluster's region in the submit payload`) under
  a new `blast submit cross-RG cluster picker` describe block.

**Infra**: no change.

## Validation evidence

* `cd web && npx tsc --noEmit` → clean.
* `cd web && npx eslint src/pages/BlastSubmit.tsx src/pages/blastSubmit/ComputeSection.tsx src/pages/blastSubmit/useClusterSelection.ts` → 0 problems.
* `cd web && npx vitest run` → **314 passed (40 files)**, including the
  two new cross-RG regression tests.
* `uv run ruff check api` → all checks passed.
* `uv run pytest -q api/tests/test_smoke.py api/tests/test_blast_database_availability.py api/tests/test_monitoring_aks_subwide.py`
  → **91 passed in 6.11s** (smoke + preflight DB availability + sub-wide
  AKS contract).

## Risk notes

* **Load-bearing contract**: the picker now relies on
  `selectedCluster.resource_group` always being populated for ELB-managed
  clusters. `_serialise_cluster` derives this from the ARM resource ID
  (`/resourceGroups/<rg>/...`), so an absent value implies a malformed ID
  and is covered by `test_subwide_returns_empty_when_resource_group_missing`
  in [api/tests/test_monitoring_aks_subwide.py](../../../api/tests/test_monitoring_aks_subwide.py).
  If this contract ever drifts, the picker would silently fall back to the
  workspace anchor RG via the `|| workloadRg` guard — visible as a
  preflight "cluster not found" check on submit, not a silent misroute.
* **No backend change** means existing operators on older SPA bundles
  continue to work unchanged — the route still accepts the anchor RG when
  the old client sends it (provided the cluster happens to be in that RG).
* `region` was already cluster-driven (`region: selectedCluster.region || region`
  in [useSubmitMutation.ts](../../../web/src/pages/blastSubmit/useSubmitMutation.ts));
  the new RG path matches that pattern.
