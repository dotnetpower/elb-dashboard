---
title: In-app Upgrades
description: How operators run the browser-driven self-upgrade flow for ElasticBLAST Control Plane — discovery, target selection, rollout, rollback, and recovery commands.
tags:
  - user-guide
---

# In-app self-upgrade

Once the dashboard is deployed, an operator can upgrade the control
plane from the browser without touching their workstation:

1. The dashboard polls the configured **git remote** for newer
   release tags every 30 minutes.
2. When a newer tag appears, the header shows an *Upgrade to vA.B.C*
   badge. Clicking the badge opens the `/upgrade` page.
3. An upgrade admin picks the target version, accepts the
   ≈ 1 minute downtime window, and starts the upgrade.
4. The api worker clones the chosen tag, runs `az acr build` for the
   three sidecar images (`elb-api`, `elb-frontend`, `elb-terminal`),
   then PATCHes the Container App template. The reconciler on the
   freshly booted revision marks the upgrade `succeeded`.
5. If the new revision misbehaves, the admin clicks **Roll back**:
   the dashboard verifies the previous tags still exist in ACR and
   PATCHes the template back. If ACR has already purged them, the
   page surfaces a copy-pasteable `az containerapp update` escape
   hatch instead.

The flow is opt-in. Until an operator sets `UPGRADE_GIT_REMOTE`, the
header badge stays hidden and every mutating endpoint refuses.

!!! info "CLI alternative when this is not available"

    When the in-browser upgrade is disabled, the SPA is down, or sidecar
    layout / Bicep changed (which the in-app upgrade does not handle), use
    the workstation-driven [CLI Rolling Update](../operate/cli-upgrade.md)
    instead. It wraps `quick-deploy.sh` / `postprovision.sh` with the same
    snapshot + health-check + auto-rollback envelope.

## Required environment variables

Set these on the deployed Container App (e.g. via
`az containerapp update --set-env-vars` or by editing the Bicep
template). All three are required for the full flow; setting only
`UPGRADE_GIT_REMOTE` enables read-only discovery without the start /
rollback buttons.

| Env | Purpose | Example | Required for |
|---|---|---|---|
| `UPGRADE_GIT_REMOTE` | HTTPS git remote that publishes the release tags the dashboard should watch. Must end in `.git`. Anonymous read access only — private remotes are out of scope. | `https://github.com/dotnetpower/elb-dashboard.git` | Discovery, build, rollback |
| `PLATFORM_ACR_NAME` | ACR name without the `.azurecr.io` suffix. Used as the `az acr build --registry` target and as the prefix when the reconciler compares image refs. | `myacr` | Build, rollback |
| `UPGRADE_ADMIN_OIDS` | **Optional break-glass override.** Comma-separated caller object IDs permitted to start / rollback / view the escape hatch. Not required: an **Owner** or **Contributor** on the deployment (subscription or resource group) is already authorised via Azure RBAC, as is the MSAL `UpgradeAdmin` app role. Use this only to grant a principal that holds neither. | `00000000-0000-0000-0000-000000000001,…` | (optional) |

### Who can start / rollback / view the escape hatch

Authorisation is granted by **any** of the following, checked in order:

1. **Azure RBAC** — the caller holds a write role (**Owner** or **Contributor**,
   including via Entra group membership) at the deployment's subscription or
   resource group. This is the default path; no extra configuration is needed.
2. **MSAL `UpgradeAdmin` app role** asserted in the caller's token.
3. **`UPGRADE_ADMIN_OIDS`** allowlist (break-glass) for a principal that holds
   neither of the above.

The RBAC check fails closed: if role enumeration cannot be performed (the
shared managed identity lacks `Microsoft.Authorization/roleAssignments/read`,
or ARM is unreachable), the caller is **not** auto-promoted — fall back to the
app role or the allowlist.

The values are read on first use, not at startup — bumping them does
not require a Container App restart.

## Setting them with `az`

```bash
RG=$(azd env get-value AZURE_RESOURCE_GROUP)
APP=$(azd env get-value CONTAINER_APP_NAME)
az containerapp update \
  --name "$APP" --resource-group "$RG" \
  --set-env-vars \
    UPGRADE_GIT_REMOTE="https://github.com/dotnetpower/elb-dashboard.git" \
    PLATFORM_ACR_NAME="$(azd env get-value PLATFORM_ACR_NAME)"
```

`UPGRADE_ADMIN_OIDS` is intentionally omitted above — an Owner/Contributor on
the deployment is already authorised via Azure RBAC. Add it only as a
break-glass override for a principal that holds neither an RBAC write role nor
the `UpgradeAdmin` app role:

```bash
az containerapp update --name "$APP" --resource-group "$RG" \
  --set-env-vars UPGRADE_ADMIN_OIDS="$(az ad signed-in-user show --query id -o tsv)"
```

## What the dashboard checks

The api sidecar's user-assigned managed identity already carries the
roles the self-upgrade needs. No additional RBAC is required:

* `acrPush` on the platform ACR — so `az acr build` can publish the
  built images.
* `Contributor` on the workspace resource group — so the api can
  PATCH the Container App template.
* `Storage Blob Data Contributor` — so the per-component build logs
  and audit history are persisted under the platform Storage
  account's `upgrade-logs` and `upgrade-history` containers.

## ACR retention

Rollback rewrites the Container App template back to the image refs
captured before the upgrade. If ACR has already pruned those tags the
rollback PATCH would still succeed and the new "rollback" revision
would crashloop on `ImagePullBackOff`. The dashboard avoids this by
calling `/api/upgrade/rollback-preflight` from the rollback card and
refusing the action when any tag is missing.

Recommended ACR retention floor for a healthy rollback window is
**90 days**. Inspect the live policy with:

```bash
az acr config retention show --registry "$PLATFORM_ACR_NAME"
```

If the rollback button reports unavailable tags, follow the escape
hatch (Recovery commands section on `/upgrade`) — it lists per-container
`az containerapp update` commands you can paste into any
`az login`-ed shell to restore the snapshot manually.

## Where state lives

* **Per-row state**: Azure Storage Table `upgradestate` (single row
  keyed `control-plane / current`). Carries the current state-machine
  node, target version, build log path, rollback snapshot, and last
  check timestamp.
* **Build logs**: Azure Blob container `upgrade-logs/<job_id>/build-<component>.log`
  (Append Blob).
* **Audit history**: Azure Blob container `upgrade-history/events.log`
  (Append Blob, one JSON event per line).

Storage is `publicNetworkAccess: Disabled` per repo invariant. The
dashboard streams these blobs back through the api sidecar; no SAS is
ever issued to the browser.

### Pruning old logs

There is no automatic retention on the `upgrade-logs` or
`upgrade-history` containers — the api stays read-only on them. A
maintainer who wants to cap storage growth can attach a lifecycle
management rule:

```bash
SA=$(azd env get-value AZURE_STORAGE_ACCOUNT)
RG=$(azd env get-value AZURE_RESOURCE_GROUP)
az storage account management-policy create \
  --account-name "$SA" --resource-group "$RG" \
  --policy '{
    "rules": [
      {
        "enabled": true, "name": "expire-old-upgrade-logs", "type": "Lifecycle",
        "definition": {
          "actions": { "baseBlob": { "delete": { "daysAfterModificationGreaterThan": 180 } } },
          "filters": { "blobTypes": ["appendBlob"],
                       "prefixMatch": ["upgrade-logs/", "upgrade-history/"] }
        }
      }
    ]
  }'
```

Each per-job build log peaks around 1 MB so even a dozen upgrades a
month stays well under any reasonable retention.
