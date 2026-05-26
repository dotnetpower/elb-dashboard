# 2026-05-27 — Always create Private Endpoints; add deploy.sh storage parity guard

## Motivation

A user reported that the "Create Cluster" wizard surfaced misleading errors
("Could not access resource group rg-elb-cluster") even though the cluster RG
existed and the dashboard managed identity had `Contributor` on it. Root-cause
investigation found the workload Storage account was in a broken state:

* `publicNetworkAccess: Disabled`
* `networkAcls.defaultAction: Allow`, `bypass: AzureServices` (the bootstrap
  pair, not the lockdown pair)
* **Zero approved Private Endpoint connections**

In that state every Container App → Storage Tables call returns
`403 AuthorizationFailure` (Azure Storage Tables returns this code for
network-policy blocks, not just RBAC denies). Every Celery task fails on the
first `JobStateRepository` write, and the SPA's ARM error classifier matches
the 403 against its "RG access denied" regex and tells the user to grant
Contributor on the cluster RG — which is the wrong fix.

The state was reachable because `infra/modules/{storage,acr,keyvault}.bicep`
gated Private Endpoint creation behind
`if (!allowPublicAccessForBootstrap)`. Bootstrap deploys created no PEs at
all; if `publicNetworkAccess` was later flipped to `Disabled` (manually, by
drift, by `storage-public-access.sh off`, or by a partial lockdown that didn't
re-run `azd provision` with `lockdownPrivateNetworking=true`), the data plane
was severed with no fallback.

## User-facing change

* Fresh `azd up` (and re-runs against existing environments) now always
  creates Private Endpoints + Private DNS zones + zone links + DNS groups for
  Storage (blob, dfs, table), ACR (registry), and Key Vault (vault). The
  Container App therefore has a private path to all three from day 1.
* The `allowPublicAccessForBootstrap` flag now ONLY controls
  `publicNetworkAccess` and `networkAcls` on the underlying resources. That
  means:
  * Bootstrap posture (`lockdownPrivateNetworking=false`, the default): public
    path open for `az acr build` / `seed-secrets` / operator workstation
    access **and** PEs already exist for the workloads.
  * Lockdown posture (`lockdownPrivateNetworking=true`): public path closed,
    PEs still there (no-op redeploy for the PE resources).
* `deploy.sh` now runs a post-`azd up` Storage parity check. If it finds
  `publicNetworkAccess: Disabled` + 0 approved PEs it prints a loud warning
  with recovery commands and surfaces a `[!] WARNING` in the closing summary.
  Re-running `azd provision` is enough to fix the state because Bicep now
  always creates the missing PEs.

## API / IaC diff summary

| File | Change |
|------|--------|
| `infra/modules/storage.bicep` | Removed `if (!allowPublicAccessForBootstrap)` from `zones`, `zoneLinks`, `endpoints`, `endpointDnsGroups`. Updated the section comment to explain the decoupling. |
| `infra/modules/acr.bicep` | Removed `if (!allowPublicAccessForBootstrap)` from `acrPrivateDnsZone`, `acrPrivateDnsLink`, `acrPrivateEndpoint`, `acrPrivateDnsGroup`. Updated section comment. |
| `infra/modules/keyvault.bicep` | Removed `if (!allowPublicAccessForBootstrap)` from `kvPrivateDnsZone`, `kvPrivateDnsLink`, `kvPrivateEndpoint`, `kvPrivateDnsGroup`. Updated section comment. |
| `deploy.sh` | After `azd up`, query the workload Storage account and emit a clear ERROR + recovery instructions when `publicNetworkAccess=Disabled` AND 0 approved PEs. Closing summary now includes a `[!] WARNING` line in that case. |
| `scripts/dev/resolve-resource-group.sh` | Re-provision short-circuit: if the current azd env's `AZURE_RESOURCE_GROUP` already points at the existing RG, skip the d/n/a prompt and let `azd provision` do its incremental update. Without this, every `azd provision` against an existing deployment misclassified the RG as a foreign collision and offered only destructive choices (delete / parallel slot / abort). |

`allowPublicAccessForBootstrap` parameter is intentionally retained because it
still drives the public/private surface of the underlying resources during
bootstrap vs. lockdown postures.

## Validation evidence

* `az bicep build --file infra/main.bicep --stdout` → succeeds (2376 lines,
  no errors, no warnings beyond the unrelated Bicep version-upgrade nag).
* `bash -n deploy.sh` → syntax OK.
* Existing pytest suite is unaffected (no Python files touched).

## Operator recovery for the affected environment

For the deployment that prompted this fix (and any other environment that was
last provisioned before this change and is now in the broken state):

```bash
# This is the canonical recovery — Bicep will create the missing PEs.
azd provision --environment elb-dashboard --no-prompt

# (Optional, only if you need the dashboard usable immediately without
# waiting for PE provisioning + DNS propagation.) Re-open the public path
# as a temporary workaround:
az storage account update --subscription <sub-id> \
    -g rg-elb-dashboard -n <storage-account> \
    --public-network-access Enabled --default-action Allow --bypass AzureServices
```
