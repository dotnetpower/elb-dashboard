# Joining An Existing Deployment From Another Machine

This page is for a developer who is **not the original deployer**. A teammate has already run `azd up` for this dashboard, and you only need to:

- sign in to the deployed dashboard URL through your browser, or
- run the SPA / backend locally against that environment.

You do **not** create a new Microsoft Entra App Registration, and you do **not** redeploy. You bind your local clone to the existing azd environment so the deployed `API_CLIENT_ID`, tenant id, subscription id, and resource ids flow into your dev tools automatically.

If something is broken instead of just unfamiliar, jump to [Troubleshooting](troubleshooting.md) first.

## What you need

- `git`, `az`, `azd`
- `uv` 0.4+ (only if you will run the local backend)
- Node.js 20+ (only if you will run the local SPA)
- An Azure account in the **same tenant** the deployment lives in
- At least `Reader` on the workload resource group — `azd env refresh` reads deployment outputs through ARM

If your account does not yet have `Reader`, ask the original deployer to grant it (`az role assignment create --assignee <upn> --role Reader --scope /subscriptions/<sub-id>/resourceGroups/<rg>`) before continuing.

## Bind your clone to the deployed environment

```bash
# 1. Sign in to the same Azure tenant the deployment lives in.
az login --tenant <TENANT_ID>
az account set --subscription <SUBSCRIPTION_ID>

# 2. Bind this clone to the existing azd environment. Use the same env name the
#    original deployer chose (the default in this repo is "elb-dashboard").
azd env refresh -e elb-dashboard

# 3. Confirm the values landed locally.
azd env get-values | grep -E '^(API_CLIENT_ID|AZURE_TENANT_ID|AZURE_SUBSCRIPTION_ID|CONTAINER_APP_URL)='
```

After step 3 you have the App Registration clientId without ever opening the Azure Portal.

## Where the values flow

| Surface | What picks the value up |
|---------|-------------------------|
| Local SPA (`scripts/dev/local-run.sh web`) | Auto-exports `VITE_AZURE_CLIENT_ID` from `API_CLIENT_ID` when `web/.env.local` leaves it empty (the new default). You do **not** edit `web/.env.local` for the clientId. |
| Local backend (`scripts/dev/local-run.sh api` / `worker` / `beat`) | Reads `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, and storage endpoint values from `.env` / azd env. |
| Deployed SPA (your browser) | Already baked in by the original `azd up`. No local action needed. |

The placeholder `00000000-0000-0000-0000-000000000000` historically shipped in `web/.env.example` no longer ships — the SPA detects it (and any non-UUID) up front and renders a **Setup Required** screen instead of failing with `AADSTS700038`. If you see that screen now, see [Troubleshooting → Setup Required](troubleshooting.md#setup-required-screen-or-aadsts700038-on-sign-in).

## If you cannot run `azd env refresh`

You may not have `Reader` on the resource group, or your org pre-creates the App Registration. In that case ask the original deployer for just the App Registration clientId and put it in `web/.env.local`:

```bash
# Lookup options for the deployer:
#   azd env get-values | grep '^API_CLIENT_ID='
#   az ad app list --display-name elastic-blast-control-plane --query "[].appId" -o tsv

# Then on the teammate's machine, in web/.env.local:
VITE_AZURE_CLIENT_ID=<paste-the-clientId>
```

Backend env values (`AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, storage endpoints) can be put in the root `.env` the same way.

## RBAC for the new teammate

The **deployed SPA** uses the shared managed identity `id-elb-dashboard-*` for all Azure calls, so a teammate who only signs in through the browser needs **no workload RBAC** of their own — the bearer token is used only for identity verification.

The **local backend** is different. `scripts/dev/local-run.sh api` uses `DefaultAzureCredential`, which falls back to your `az login` identity. A brand-new account starts with zero RBAC on the workload Storage / ACR / RG, so the dashboard renders `access_denied` / `network_blocked` cards and DB downloads fail with HTTP 403.

There are two equivalent ways to grant the minimum role set (`Storage Blob Data Contributor`, `Storage Table Data Contributor`, `Storage Account Contributor`, `Reader` on the workload RG, `AcrPull` on the workload ACR):

```bash
# A. Teammate runs it on their own laptop after `az login`.
#    Requires the teammate to already hold `User Access Administrator` (or higher)
#    on the workload RG — most non-Owner accounts cannot do this themselves.
scripts/dev/grant-local-rbac.sh                 # add --dry-run to preview

# B. Deployer runs it from their own laptop and targets the teammate's account.
#    This is the usual path when the teammate cannot self-grant.
scripts/dev/grant-local-rbac.sh --user teammate@contoso.onmicrosoft.com
```

Both invocations accept `--storage`, `--storage-rg`, `--acr`, `--acr-rg`, and `--subscription` overrides when the deployment uses non-default names. Wait 1-5 minutes for RBAC propagation before re-running the local api. The script never revokes; use `az role assignment delete` for that.

## Driving the deployed Storage account from your laptop

When you debug a deployed Storage account from your laptop, the workload Storage stays `publicNetworkAccess: Disabled` by default and the dashboard renders the `network_blocked` degraded state. To open a short IP-allowlisted window for your caller IP only, follow [Driving a deployed environment from your laptop](https://github.com/dotnetpower/elb-dashboard#driving-a-deployed-environment-from-your-laptop) in `README.md`. Always close the window with `scripts/dev/storage-public-access.sh off` (or `local-run.sh storage-off`) when you finish.

## Where to go next

- [Troubleshooting](troubleshooting.md) — symptom-first index for the errors a new teammate is most likely to hit.
- [User Guide](user-guide/index.md) — day-to-day operation once sign-in works.
- [Deployment Reference](deployment-reference.md) — only needed if you eventually become a deployer yourself.
