---
title: Troubleshooting
description: Symptom-first guide to the most common sign-in, RBAC, Storage network, and upgrade errors when running the ElasticBLAST Control Plane.
tags:
  - setup
---

# Troubleshooting

Symptom-first index for the errors most teams hit while signing in to or driving the dashboard. Each section is self-contained — start with the heading that matches what you see on screen or in a log.

For onboarding-time questions (how do I find the App Registration clientId, how do I grant RBAC to a teammate, etc.) start with [Joining An Existing Deployment](joining-existing-deployment.md) instead. This page is for things that are already broken.

## Setup Required screen, or AADSTS700038 on sign-in

**Symptom**

- The SPA renders a "Setup Required" glass card instead of the Sign in page, OR
- The Microsoft sign-in popup reports `AADSTS700038: 00000000-0000-0000-0000-000000000000 is not a valid application identifier` (the UUID may also be any non-UUID string).

**Cause**

The SPA was built with no `VITE_AZURE_CLIENT_ID`, or with the placeholder all-zero UUID that historically shipped in `web/.env.example`. The build sent that placeholder to Microsoft Entra and Entra rejected it.

**Fix**

1. If you are running locally, bind your clone to the existing azd environment:

    ```bash
    azd env refresh -e elb-dashboard
    scripts/dev/local-run.sh web
    ```

    `local-run.sh web` auto-exports `VITE_AZURE_CLIENT_ID` from `API_CLIENT_ID` in azd env. You do not edit `web/.env.local` for the clientId.

2. If you cannot run `azd env refresh`, paste the clientId directly into `web/.env.local`:

    ```bash
    VITE_AZURE_CLIENT_ID=<paste-the-clientId>
    ```

3. If the Container App rendered this in a deployed environment, it means `API_CLIENT_ID` was empty when the frontend image was built. Re-run `azd provision` (or `scripts/dev/postprovision.sh`) so the App Registration is created/resolved and `--build-arg VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL` reaches the next `az acr build`.

Full clientId discovery flow: [Joining An Existing Deployment → Bind your clone](joining-existing-deployment.md#bind-your-clone-to-the-deployed-environment).

## Sign-in succeeds but Dashboard cards show "access_denied"

**Symptom**

You signed in fine through the deployed SPA, but one or more cards (Storage, ACR, AKS, BLAST Databases) shows `access_denied`. Browser DevTools shows HTTP 403 with `AuthorizationPermissionMismatch` from a Storage / ARM endpoint.

**Cause**

The deployed SPA itself uses the shared managed identity for Azure calls, so seeing `access_denied` in the deployed surface usually means the **MI** lost a role assignment (most often after `azd down` followed by a fresh `azd up`, which creates a new MI object id).

If you are running the **local** backend instead, `DefaultAzureCredential` is using your `az login` identity, and your account has no RBAC on the workload Storage / ACR / RG yet.

**Fix — deployed dashboard (MI lost roles)**

Re-run the MI role checklist:

```bash
source <(azd env get-values -e <YOUR_ENV> | sed 's/^/export /')
# Then re-run the role assignments from docs/auth.md §0.
```

Full checklist: [Auth → §0 Post-Deploy Permissions Checklist](architecture/authentication.md#0-post-deploy-permissions-checklist-run-after-every-azd-up).

**Fix — local backend (your account has no roles)**

```bash
# A. Self-grant (you need User Access Administrator on the workload RG).
scripts/dev/grant-local-rbac.sh                          # add --dry-run to preview

# B. Deployer grants to a teammate's account.
scripts/dev/grant-local-rbac.sh --user teammate@contoso.onmicrosoft.com
```

Wait 1-5 minutes for RBAC propagation, then restart `scripts/dev/local-run.sh api`.

Full RBAC story: [Joining An Existing Deployment → RBAC for the new teammate](joining-existing-deployment.md#rbac-for-the-new-teammate).

## Dashboard cards show "network_blocked"

**Symptom**

Storage-backed cards (BLAST Databases, Queries, Results) show `network_blocked`. The deployed dashboard itself works.

**Cause**

The workload Storage account has `publicNetworkAccess: Disabled` (the production default). The deployed Container App reaches Storage over private endpoints from inside the VNet, but your laptop cannot reach the private endpoint. This is **expected** for the deployed dashboard rendered from a laptop, and for the local backend when run from outside the VNet.

**Fix**

Use the explicit local-debug helper to open a short IP-allowlisted window for your caller IP only — never `defaultAction: Allow`, never `bypass: AzureServices`:

```bash
scripts/dev/local-run.sh storage-on        # publicNetworkAccess=Enabled with defaultAction=Deny + your IP in ipRules
# ... debug ...
scripts/dev/local-run.sh storage-off       # restore publicNetworkAccess=Disabled
```

Status check:

```bash
scripts/dev/local-run.sh storage-status
```

The helper refuses to run inside a Container App (`CONTAINER_APP_NAME` guard), so it cannot accidentally weaken production. The local backend may also auto-open with `LOCAL_DEBUG_AUTO_OPEN_STORAGE=true` — see [`.github/copilot-instructions.md §9`](https://github.com/dotnetpower/elb-dashboard/blob/main/.github/copilot-instructions.md#9-storage-network-isolation-hard-requirement).

Do not leave the network surface open after debugging. The Storage card itself shows the current `publicNetworkAccess` value so you can confirm it is back to `Disabled`.

## Sign-in works but Dashboard shows no workspace

**Symptom**

You signed in, no error message, but the Dashboard shows the empty Setup Wizard ("Select your subscription / resource group / Storage account / ACR") instead of a workspace.

**Cause**

The dashboard discovers workspaces by scanning subscriptions for a Storage account tagged for ElasticBLAST. Either:

- your account does not have `Reader` on the workload subscription, or
- the Storage account is missing the expected tag, or
- the workspace was deployed in a different subscription than the one selected by `az account set`.

**Fix**

1. Confirm your tenant / subscription:

    ```bash
    az account show --query '[tenantId,id]' -o tsv
    ```

    It must match the tenant the deployment lives in.

2. Confirm `Reader` on the workload subscription (ask the deployer to grant if missing):

    ```bash
    az role assignment list --assignee <upn> --scope /subscriptions/<sub-id> -o table
    ```

3. Use the Setup Wizard once to pick the subscription / resource group / Storage account / ACR explicitly. The selection persists per browser.

## Sign-in popup blocked, or redirect URI mismatch (AADSTS50011)

**Symptom**

- The popup closes without signing in, or
- Entra reports `AADSTS50011: The reply URL specified in the request does not match the reply URLs configured for the application`.

**Cause**

The Container App URL was not registered as a SPA redirect URI on the App Registration. This can happen if you redeployed to a new resource group or renamed the Container App.

**Fix**

`scripts/dev/postprovision.sh` adds the deployed Container App origin automatically. To do it by hand, follow [Deployment Reference → Redirect URI After Deployment](deployment-reference.md#redirect-uri-after-deployment).

Keep `http://localhost:8090` registered as well if you also run the SPA locally.

## Local `scripts/dev/local-run.sh web` does not pick up the clientId

**Symptom**

You ran `azd env refresh`, then `scripts/dev/local-run.sh web`, but the SPA still shows "Setup Required".

**Cause**

The auto-pull only triggers when `VITE_AZURE_CLIENT_ID` is empty or the all-zero placeholder. A stale `web/.env.local` from an older clone may have a non-empty value baked in.

**Fix**

```bash
# Check what is actually exported.
grep '^VITE_AZURE_CLIENT_ID' web/.env.local

# Either delete the line (auto-pull will fill it from azd env), or paste the correct value.
azd env get-values | grep '^API_CLIENT_ID='
```

Then restart `scripts/dev/local-run.sh web`. The log line `[local-run] Picked up VITE_AZURE_CLIENT_ID from azd env (...)` on stderr confirms the auto-pull fired.

## Local debug as your real az-login identity (one-shot)

**Symptom**

You want the local dashboard caller chip to show your real UPN instead of `anonymous`, and you want the BLAST Databases / Storage cards to actually load data (not `degraded access_denied`) — without running four scripts by hand every session.

**Cause**

The default local dev mode flips `AUTH_DEV_BYPASS=true` (anonymous caller) and the workload Storage account starts with `publicNetworkAccess: Disabled` plus zero RBAC on your `az login` identity. Three things have to flip together for real auth to work end-to-end:

1. **RBAC** — your account needs `Storage Blob/Table Data Contributor` (+ `Reader` on the RG, `AcrPull` on ACR).
2. **Storage network** — the account must be reachable from your laptop (the explicit local-debug allowlist, never `defaultAction: Allow` in production).
3. **Bypass off** — `AUTH_DEV_BYPASS=false` in `.env` and `VITE_AUTH_DEV_BYPASS=false` in `web/.env.local`, plus a restart of `api` + `vite`.

**Fix — single command**

```bash
# Enable real MSAL login + ensure RBAC + open storage + restart api/web.
scripts/dev/local-debug-auth.sh on
# or:
scripts/dev/local-run.sh auth-on

# When you finish: revert to anonymous bypass + close storage network.
# RBAC is intentionally NOT revoked (cheap to keep).
scripts/dev/local-run.sh auth-off

# Print current state without mutating anything.
scripts/dev/local-run.sh auth-status
```

The script is idempotent and re-runnable. It auto-detects the workload storage account, ACR, and `API_CLIENT_ID` from `azd env get-values`; pass `--storage NAME --storage-rg RG` to target a specific deployment when multiple `stelbdashboard*` accounts exist in your subscription.

Useful flags:

| Flag | Effect |
|------|--------|
| `--storage NAME --storage-rg RG` | Target a specific deployment (when azd env default ≠ the one your SPA uses). |
| `--acr NAME --acr-rg RG` | Override the ACR used for the `AcrPull` role assignment. |
| `--skip-rbac` | Skip the role-assignment step (if RBAC is already verified). |
| `--skip-storage` | Skip the storage network toggle (if you already opened it). |
| `--skip-restart` | Apply env changes only; restart `api` + `vite` yourself. |
| `--no-close-storage` | (`off` only) leave storage open; only flip the bypass flags. |

Permission requirements:

* `az login` as a user with **Storage Blob Data Contributor** and **User Access Administrator** (or **Owner**) on the workload Storage account scope. The script pre-checks `az role assignment list` and fails fast if you cannot read assignments at that scope.
* `Microsoft.Storage/storageAccounts/write` on the account (for the network toggle).
* `jq` and `curl` on `PATH` (already required by sibling dev scripts).

After `auth-on` succeeds, open <http://localhost:8090>, complete the MSAL sign-in, and the caller chip should now show your UPN. `/api/me` will return your real `oid` / `upn` instead of the synthetic `00000000-…` dev-bypass identity.

**Charter §9 reminder — close the network when done.** `publicNetworkAccess: Enabled` is a transient local-debug state. Running `auth-off` is enough; if you only want to close the network, `scripts/dev/local-run.sh storage-off` works.

## In-app upgrade flow

### The header badge never appears

Set `UPGRADE_GIT_REMOTE` on the deployed Container App and wait for the
30-minute discovery beat (or hit **Check remote** on `/upgrade`). The
URL must end in `.git` and resolve to a public HTTPS endpoint. The
upgrade subsystem is intentionally inert until the env is set —
[upgrades.md](user-guide/upgrades.md) has the full env table.

### "Start" returns 403

Your caller `oid` is not in `UPGRADE_ADMIN_OIDS` and you do not carry
the `UpgradeAdmin` app role. Add your oid to the env (comma-separated)
or grant the app role:

```bash
RG=$(azd env get-value AZURE_RESOURCE_GROUP)
APP=$(azd env get-value CONTAINER_APP_NAME)
MY_OID=$(az ad signed-in-user show --query id -o tsv)
EXISTING=$(az containerapp show --name "$APP" --resource-group "$RG" \
  --query "properties.template.containers[?name=='api'].env[?name=='UPGRADE_ADMIN_OIDS'].value | [0]" -o tsv)
az containerapp update --name "$APP" --resource-group "$RG" \
  --set-env-vars UPGRADE_ADMIN_OIDS="${EXISTING:+$EXISTING,}$MY_OID"
```

### "Start" returns 409 — `upgrade already in progress`

`upgradestate` row is not `idle`. Inspect the row state on the
`/upgrade` page; if a previous attempt left it in `failed_pre` or
`failed_rollout`, transition it back to `idle` by clicking **Rollback**
(if a snapshot exists) or by clearing the row manually with the Azure
Storage Explorer / `az storage entity replace`.

### Upgrade stayed in `rolling_out` past the budget

The reconciler's stuck guard moves the row to `failed_rollout` after
15 minutes — or as fast as 2 minutes when the ACA template clearly
does not carry the target version. If the new revision is actually
unhealthy:

1. Read the per-component build log on `/upgrade` (or
   `curl /api/upgrade/jobs/<job_id>/build-log/api`).
2. Click **Rollback**; the dashboard refuses if ACR no longer carries
   the snapshot tags — see the next section.
3. If even the api sidecar is unreachable, copy the **Recovery
   commands** from `/upgrade` (or `/api/upgrade/escape-hatch`) and
   paste them into any `az login`-ed shell.

### Rollback says "ACR no longer carries the snapshotted tags"

Retention has purged at least one of the per-sidecar image tags. The
rollback PATCH would succeed but ACA would crashloop on
`ImagePullBackOff`. Recovery options:

* Re-build the older release locally — `azd up` from a checkout of the
  prior git tag rebuilds the missing tags with the same names.
* Or pick a *forward* upgrade to a known-good newer tag instead of
  rolling back.

Bump ACR retention so the next rollback succeeds:

```bash
az acr config retention update \
  --registry "$(azd env get-value PLATFORM_ACR_NAME)" \
  --status enabled --days 180 --type UntaggedManifests
```

### Build logs are empty / 404

The Append Blob is only created when the `az acr build` for that
component actually starts. If the upgrade `failed_pre` during clone or
before the `building` state, no log was produced. Inspect the row's
`phase_detail` and the audit history (`/api/upgrade/history`).

## `azd env refresh` fails with "no environment selected"

**Symptom**

```text
no default environment, run `azd env new` to create one
```

**Cause**

The clone has never had an azd environment created. `azd env refresh` only binds an environment that already exists in your local clone.

**Fix**

```bash
azd env new elb-dashboard          # same name the original deployer used
azd env refresh -e elb-dashboard
```

`azd env new` creates the local stub; `azd env refresh` then fills it from the deployment outputs.

## OpenAPI "Try it" / Service Bus drain unreachable after a manual cluster recreate

**Symptom**

- The API page shows the OpenAPI spec as degraded (`openapi_endpoint_unreachable` / `openapi_service_not_reachable`), and the Service Bus drain path cannot reach the execution plane.
- `kubectl describe svc elb-openapi -n default` shows the Service stuck on `EXTERNAL-IP <pending>` with an event like:

    ```text
    Warning  SyncLoadBalancerFailed  service-controller
      Error syncing load balancer: failed to ensure load balancer:
      GET .../virtualNetworks/<platform-vnet>/subnets/snet-aks
      RESPONSE 403: AuthorizationFailed ... does not have authorization to
      perform action 'Microsoft.Network/virtualNetworks/subnets/read'
    ```

- The `elb-openapi` pod itself is healthy (`READY 1/1 Running`); only the internal LoadBalancer frontend IP never gets allocated.

**Cause**

This is a [bring-your-own (BYO) subnet](https://learn.microsoft.com/azure/aks/configure-azure-cni) AKS cluster: the nodes live in the dashboard's `vnet-elb-dashboard/snet-aks`. The **runtime** LoadBalancer reconcile runs as the *cluster control-plane identity* (not the dashboard managed identity that created the nodes), so that identity needs **Network Contributor** on the `snet-aks` subnet to allocate the internal LB frontend IP.

The dashboard's `provision_aks` task grants this automatically at create time, and the **Deploy elb-openapi** task (`deploy_openapi_service`) also grants it just before it applies the Service. A cluster whose `elb-openapi` was applied **before** that grant integration shipped (or applied entirely out-of-band) can still be missing it.

**Fix**

The simplest fix is to **re-run Deploy elb-openapi** from the API page (or `POST /api/aks/openapi/deploy`) — the deploy task now performs the grant before re-applying the Service, so the LoadBalancer comes up on the next reconcile.

If you cannot redeploy, re-run just the grant idempotently via the recovery route:

```bash
APP=https://<your-container-app-fqdn>
CID=<API_CLIENT_ID>
TOKEN=$(az account get-access-token --resource "$CID" --query accessToken -o tsv)
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"subscription_id":"<sub>","resource_group":"<cluster-rg>","cluster_name":"<cluster>"}' \
  "$APP/api/aks/openapi/lb-subnet-rbac"
```

A `granted` response means the role is now (or already) assigned. A `skipped` response with `managed_vnet_mode` means the cluster is not BYO-subnet (AKS manages the LB itself — nothing to fix here).

!!! warning "A grant on an already-running cluster does not take effect instantly"
    The AKS cloud-controller caches its ARM token, so a role granted on an
    **already-running** cluster is not seen until that token refreshes — the
    LoadBalancer can stay `<pending>` for several minutes. If it does not
    recover on its own, **stop and start the cluster** to force the
    cloud-controller to pick up the new role. Both the provision-time grant and
    the Deploy-elb-openapi grant avoid this because they run before the Service
    is (re)created, so the cloud-provider sees the role on its first reconcile.


## Where to go next

- [Joining An Existing Deployment](joining-existing-deployment.md) — happy path for the same workflow.
- [Auth](architecture/authentication.md) — full RBAC matrix for the managed identity, and the post-deploy permissions checklist.
- [Deployment Reference](deployment-reference.md) — manual `azd` flow, redirect URI setup, lockdown.
