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

Full checklist: [Auth → §0 Post-Deploy Permissions Checklist](auth.md#0-post-deploy-permissions-checklist-run-after-every-azd-up).

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

## Where to go next

- [Joining An Existing Deployment](joining-existing-deployment.md) — happy path for the same workflow.
- [Auth](auth.md) — full RBAC matrix for the managed identity, and the post-deploy permissions checklist.
- [Deployment Reference](deployment-reference.md) — manual `azd` flow, redirect URI setup, lockdown.
