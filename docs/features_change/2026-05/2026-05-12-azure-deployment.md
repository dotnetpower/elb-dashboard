# Azure Deployment — Function App + SWA + Entra ID Auth

**Date**: 2026-05-12

## Motivation

First production deployment of the control plane to Azure. Required infrastructure provisioning, Entra ID authentication setup, and handling the subscription's Azure Policy constraint on storage shared key access.

## Deployed Resources

| Resource | Name | Region | URL |
|----------|------|--------|-----|
| Function App | `func-elb-prod-ga5754pr7jw3u` | koreacentral | https://func-elb-prod-ga5754pr7jw3u.azurewebsites.net |
| Static Web App | `stapp-elb-prod-ga5754pr7jw3u` | eastasia | https://kind-coast-0eb698500.7.azurestaticapps.net |
| Key Vault | `kv-ga5754pr7jw3u` | koreacentral | |
| Storage | `stelbga5754pr7jw3u` | koreacentral | |
| App Insights | `appi-elb-prod-ga5754pr7jw3u` | koreacentral | |
| Resource Group | `rg-elb-prod` | koreacentral | |

## Authentication Flow

```
Browser → MSAL.js (Auth Code + PKCE) → Microsoft Entra ID
       → Access token for api://14cf2a04-...
       → Authorization: Bearer <token> → Function App
       → validate_bearer_token() (custom JWT, RS256, OIDC discovery)
       → OnBehalfOfCredential → ARM API (user's RBAC identity)
```

- **Easy Auth**: Enabled on Function App, `AllowAnonymous` mode (SPA controls the flow)
- **Custom JWT**: `token.py` validates RS256, audience, issuer, required claims
- **OBO**: `API_CLIENT_SECRET` stored in Key Vault, referenced via `@Microsoft.KeyVault(SecretUri=...)`
- **App Registration**: `elastic-blast-control-plane` (client ID: `14cf2a04-9985-4372-aa68-8d54c9adb75a`)
- **SPA Redirect URIs**: `http://localhost:8090` (dev), `https://kind-coast-0eb698500.7.azurestaticapps.net` (prod)

## Infrastructure Changes (Bicep)

| File | Change |
|------|--------|
| `infra/main.bicep` | Added `@secure() param apiClientSecret` |
| `infra/modules/platform.bicep` | Added `@secure() param apiClientSecret`, Easy Auth `authsettingsV2` resource, `apiClientSecretKv` Key Vault secret, SWA region fallback (`koreacentral` → `eastasia`) |

## Deployment Constraint & Solution

**Problem**: Subscription Azure Policy enforces `allowSharedKeyAccess: false` on all storage accounts. This blocks:
- `func azure functionapp publish` (uses storage key internally)
- `az functionapp deployment source config-zip` without `--build-remote`
- Any SAS URL generated with account key

**Solution** (keyless deployment path):
```bash
# 1. Assign Storage Blob Data Contributor to deployer (one-time, ~10min propagation)
az role assignment create --assignee-object-id <oid> --role "Storage Blob Data Contributor" --scope <storage-id>

# 2. Upload zip via Entra ID (no key needed)
az storage blob upload -f package.zip -c function-releases -n package.zip \
  --account-name <account> --auth-mode login

# 3. Generate User Delegation SAS (max 7 days)
SAS_URL=$(az storage blob generate-sas -c function-releases -n package.zip \
  --account-name <account> --as-user --auth-mode login \
  --permissions r --expiry <+7d> --full-uri -o tsv)

# 4. Set run-from-package
az functionapp config appsettings set --settings "WEBSITE_RUN_FROM_PACKAGE=$SAS_URL"
az functionapp restart
```

**Note**: User Delegation SAS expires in 7 days max. For CI/CD, use Function App's managed identity with direct blob read access instead.

## Production Hardening Applied

| Category | Change |
|----------|--------|
| Response headers | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Cache-Control: no-store`, `Referrer-Policy`, `Permissions-Policy` on all API responses |
| SWA headers | CSP, HSTS, X-Frame-Options, Referrer-Policy via `staticwebapp.config.json` |
| SWA routing | `/api/*` route rule added, removed from `navigationFallback.exclude` |
| CORS | SWA hostname added to Function App CORS allowed origins |
| Extension bundle | Upgraded from `[4.17.0, 4.18.0)` to `[4.*, 5.0.0)` |
| Build | `.env.production` created to override `.env.local` localhost references |

## Validation

- Function App: HTTP 401 on unauthenticated requests (auth working)
- SWA: "Sign in with Microsoft" page renders correctly
- App Insights: traces show function host started, functions loaded
- 13 unit tests pass, TypeScript build clean
