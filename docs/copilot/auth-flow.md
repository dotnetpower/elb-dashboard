---
title: Auth Flow (Agent Detail)
description: Implementation-level walkthrough of browser MSAL, optional shared-token M2M authentication, managed identity downstream calls, and terminal identity isolation.
tags:
  - agent
  - auth
---

# Authentication Flow (detail)

> Re-verified 2026-09-16 against `api/auth.py`, the shared deployment defaults,
> and `terminal/entrypoint.sh`.

1. SPA loads → MSAL acquires an **ID token + access token** for the app's API audience via Auth Code + PKCE.
2. SPA calls `/api/*` with `Authorization: Bearer <access_token>`.
3. The `api` sidecar (FastAPI) validates the JWT (issuer, audience, signing keys cached from the tenant's OpenID metadata) **before** any business logic runs. Trusted automation may instead send `X-ELB-API-Token` when `ALLOW_OPENAPI_TOKEN_AUTH=true`; the shared deployment default is ON, while an unset process fallback is OFF. A wrong presented shared token fails with 401 and never falls through to bearer auth.
4. For ARM and data-plane calls, the backend uses the **shared user-assigned Managed Identity** `id-elb-dashboard-*` (mounted on the Container App and visible to all six sidecars) via `DefaultAzureCredential`. The bearer token is used only for identity verification (who is calling), not for downstream Azure calls. This avoids OBO consent issues and removes the need for `API_CLIENT_SECRET`.
5. The MI must be pre-granted sufficient RBAC roles (see [docs/architecture/authentication.md](../architecture/authentication.md) §1 for the full matrix). Runtime role assignments (e.g. granting AcrPull to AKS kubelet) are best-effort — if the MI lacks `User Access Administrator`, the code logs a one-line `az role assignment create` recovery hint instead of failing.
6. The `terminal` sidecar bootstraps `az login --identity --client-id $AZURE_CLIENT_ID` for the programmatic exec cache used by api/worker CLI calls. Browser shells use separate per-operator `AZURE_CONFIG_DIR` directories and require `az login --use-device-code` (or an explicit identity login) on first use. `/home/azureuser` is **ephemeral**, so interactive login state disappears on a revision swap; the exec cache is recreated automatically at startup.

> **Design choice**: We intentionally use Managed Identity instead of OBO. OBO requires `API_CLIENT_SECRET` and multi-resource consent, which are fragile in single-tenant research environments. MI simplifies deployment at the cost of the MI needing broad permissions — acceptable because the MI is scoped to the Container App and auditable via Azure Monitor.

## Troubleshooting: workspace cards show "Ready/OK" but data looks wrong

When the dashboard's AKS / Storage / ACR cards show "Ready" or "OK" but the
fields underneath are blank (`SKU: ?`, `IMAGES BUILT: 0/0`), the most likely
cause is one of:

1. **Stale wizard settings.** `SetupWizard` saves the chosen Subscription /
   Resource Group / ACR / Storage names to `localStorage`. If the targeted
   resources were renamed or deleted, the SPA keeps calling the old names and
   the backend responds with `{"degraded": true, "degraded_reason":
   "not_found", ...}`.
2. **Wrong Azure tenant.** The configured subscription belongs to a different
  tenant than the credential used by the backend. In a deployed revision that
  means a deployment/config mismatch; in host-mode development it commonly
  means the developer's `az login` or `AZURE_CONFIG_DIR` points elsewhere. ARM returns
   `InvalidAuthenticationTokenTenant`, the monitor router classifies it as
   `auth_wrong_tenant`, and the backend returns an empty payload.
3. **Missing role assignment.** The dashboard MI lacks the management/data
  role needed by the card, or the caller fails the dashboard entry RBAC gate.
  See [Authentication & Authorization](../architecture/authentication.md).

### What the SPA does automatically

* `MonitorCard` chips no longer say "OK" / "Ready" when the payload is
  degraded — they switch to "Wrong tenant", "No access", "Not found", etc.,
  with the full reason in the tooltip.
* `WorkspaceDiagnosticsBanner` (above the dashboard grid) renders only when
  the issue is workspace-wide: a single forbidden card on one leaf resource
  is left to that card alone, but any `auth_wrong_tenant` or two-or-more
  cards with auth/not-found issues triggers the banner.
* The banner's **Reset workspace** button calls `clearConfig()` and re-runs
  the setup wizard so the user can re-enter the right values from scratch.

### How to fix it manually

```bash
# Case 1 — stale wizard settings
# In the SPA: click "Reset workspace" in the diagnostics banner, or in the
# browser devtools console:
localStorage.clear(); location.reload();

# Case 2 — wrong tenant
az login --tenant <correct-tenant-id>
az account set --subscription <subscription-id-shown-in-spa>

# Case 3 — missing role
# Use /api/me/access-review or scripts/dev/check-mi-rbac.sh to identify whether
# the caller or dashboard MI is missing a role, then grant it at the target RG/resource.
```

### Backend taxonomy

The classification is owned by
[`api/routes/monitor/common.py::_classify_exception`](../../api/routes/monitor/common.py)
and consumed by
[`web/src/utils/monitorDegraded.ts`](../../web/src/utils/monitorDegraded.ts).
Codes are part of the SPA contract; renaming one requires changing both
files together. Pinned codes:

| Reason | When | UI label |
|---|---|---|
| `auth_wrong_tenant` | ARM 401 with `InvalidAuthenticationTokenTenant` / `AADSTS50020` | "Wrong tenant" (danger) |
| `unauthorized` | ARM 401 (other) | "Auth required" (danger) |
| `forbidden` | ARM 403 | "No access" (warning) |
| `not_found` | ARM 404 / `ResourceNotFoundError` | "Not found" (muted) |
| `azure_error` | Other `AzureError` | "Azure error" (warning) |
| `http_<status>` | Other HTTP errors | "Degraded" (warning) |
