# AKS Provisioning — Allow Reusing an Existing Resource Group

## Motivation

Creating a second AKS cluster into an already-existing resource group was
blocked by the modal with a hard `Pick a different name` warning. This was
wrong:

- Azure allows multiple AKS clusters in a single resource group.
- The backend `provision_aks` task already calls
  `rc.resource_groups.get(rg)` first and only `create_or_update`s on
  miss, so re-using an existing RG is idempotent.
- The dashboard's default RG name (`rg-elb-cluster`) is intentionally
  shared across the `elb-cluster-NN` family, so the *first* additional
  cluster always hit the warning.

## User-facing change

In the **Create AKS Cluster** modal:

- The Resource Group field no longer turns yellow when the RG already
  exists, and the `Create Cluster` button is no longer disabled in
  that case.
- The yellow blocking warning ("A resource group named X already exists…
  Pick a different name.") is replaced with a neutral info note:
  > Resource group `rg-elb-cluster` already exists — it will be reused.
  > Multiple AKS clusters can share a single resource group.
- When the typed RG name does *not* match an existing RG, the helper text
  now reads "New resource group will be created." (instead of the previous
  "Name is available.").

## API / IaC diff

- `web/src/components/cards/ClusterCard/useClusterProvisioning.ts`
  - Renamed `provisionResourceGroupConflict` →
    `provisionResourceGroupExists` to reflect that it is now an
    informational signal, not a blocker.
  - Removed the `conflict` early-return in `handleProvision`.
- `web/src/components/cards/ClusterCard/ProvisionModal.tsx`
  - Renamed prop `resourceGroupConflict` → `resourceGroupExists`.
  - Removed `resourceGroupConflict` from the submit guard and from the
    `Create Cluster` button's `disabled` condition.
  - Removed the warning border color on the RG input.
  - Replaced the warning copy with the neutral "will be reused" note.
- `web/src/components/cards/ClusterCard/ClusterCard.tsx`
  - Passes the renamed prop through.

No backend, no infra, no test changes.

## Validation

- `cd web && npm run build` — built in 6.93 s, no TypeScript errors.
- Manual: open the modal with a default RG that already exists; the
  Create button is now enabled and the info note appears below the
  field.
