# Hide AKS-managed RGs from workspace selectors

## Motivation

When AKS provisions a cluster it auto-creates a separate "node" resource
group that holds VMSS / NICs / disks / NSGs. By default this RG is named
`MC_<workloadRG>_<clusterName>_<region>` and is owned by AKS — users
must never select it as an ElasticBLAST workspace because the dashboard
cannot manage it independently of the parent AKS resource.

The auto-discovery flow was nevertheless surfacing the MC_ RG in two
places:

1. The **BLAST Workspaces Found** picker on first load listed
   `MC_rg-elb-01_elb-cluster_koreacentral` alongside the real
   `rg-elb-01` workspace, because the MC_ RG happened to inherit at
   least one `elb-*` tag.
2. The dashboard top-bar **Workload RG** dropdown also offered the MC_
   RG as a selectable option.

## User-facing change

* The workspace picker now omits AKS-managed node RGs entirely (they
  are not workspaces — there is no useful "pick" action for them).
* The Workload RG dropdown still shows AKS-managed RGs (so users can
  see they exist) but renders them disabled with the description
  `<location> · AKS-managed (node RG)`.

## Implementation

* New helper [web/src/lib/aksManagedRg.ts](../../../web/src/lib/aksManagedRg.ts)
  exports `isAksManagedResourceGroup({ name, tags })`. It flags any RG
  carrying the `aks-managed-cluster-name` tag (definitive — set by AKS
  itself) **or** whose name starts with `MC_` (default node-RG naming).
* [web/src/pages/Dashboard/configFromTags.ts](../../../web/src/pages/Dashboard/configFromTags.ts)
  short-circuits to `null` for AKS-managed RGs, so the workspace picker
  never sees them.
* [web/src/components/ConfigBar.tsx](../../../web/src/components/ConfigBar.tsx)
  classifies each RG into `aks-managed` / `no elb-* tag` / `selectable`
  and disables the first two with appropriate descriptions.

## Validation

* `npx vitest run src/lib/aksManagedRg.test.ts` — 4 tests pass (MC_
  prefix, `aks-managed-cluster-name` tag, ordinary workspace RG, false
  positive on `rg-MC_…` middle-of-name).
* `npx tsc --noEmit` — clean.
