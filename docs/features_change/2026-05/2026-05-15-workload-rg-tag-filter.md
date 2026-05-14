# Workload RG dropdown — disable RGs without elb-* tags

## Motivation
The Dashboard config strip's **Workload RG** dropdown listed *every* resource
group in the selected subscription. Picking an RG that was not provisioned by
this dashboard (no `elb-*` tags) silently produces a half-configured workspace:
`getRgTags` returns no useful values, so the auto-load of ACR / storage /
terminal pointers does nothing and the cards immediately render "Not found".

Users (especially in shared subscriptions) hit this regularly because RG names
are not self-describing.

## User-facing change
- The Workload RG dropdown now renders every RG, but **only RGs that carry at
  least one `elb-*` tag are selectable**.
- Untagged RGs are still visible (so users can see they exist) but show as
  greyed-out with the suffix `· no elb-* tag` and cannot be picked.
- Selectable (tagged) RGs sort to the top of the list.
- Auto-select (first item when no value is set) now skips disabled options, so
  the picker no longer lands on a non-manageable RG.

The same convention is used by the existing auto-discovery flow in
`Dashboard.tsx` (`configFromTags` qualifies an RG when any key starts with
`elb-`) and by the backend `arm.py` (`ELB_TAG_PREFIX = "elb-"`), so behaviour
is consistent across discovery, dropdown, and tag read/write.

## API / IaC diff summary
- `web/src/components/ResourcePicker.tsx`: `Item` gains an optional
  `disabled?: boolean`; both compact and non-compact `<option>` renderers honour
  it; the auto-select effect now picks the first **non-disabled** item.
- `web/src/components/ConfigBar.tsx`: `rgFetcher` inspects tags on each RG,
  marks the option `disabled` when no `elb-*` tag is present, adds a
  `no elb-* tag` description for clarity, and sorts enabled RGs first.
- No backend, no IaC, no tag schema changes. `armProxyApi.listResourceGroups`
  already returns `tags` (see `ArmResourceGroup` in `web/src/api/endpoints.ts`
  and `api/routes/arm.py::list_resource_groups`).

## Validation evidence
- `npx tsc --noEmit -p .` (in `web/`): the two touched files report no errors
  (`get_errors` clean). Two pre-existing `ClusterCard.tsx` errors about
  `vCPUs`/`memoryGiB` are unrelated to this change.
- Manual: with `AUTH_DEV_BYPASS=true` the dev backend at `:8080` returns a
  mixed set of RGs from `armProxyApi.listResourceGroups`; in the SPA the
  Workload RG dropdown now greys out RGs that lack any `elb-*` tag and keeps
  tagged RGs selectable at the top.
