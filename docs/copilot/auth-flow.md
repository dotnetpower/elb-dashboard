# Authentication Flow (detail)

> Extracted from `.github/copilot-instructions.md` §5 on 2026-05-19.

1. SPA loads → MSAL acquires an **ID token + access token** for the app's API audience via Auth Code + PKCE.
2. SPA calls `/api/*` with `Authorization: Bearer <access_token>`.
3. The `api` sidecar (FastAPI) validates the JWT (issuer, audience, signing keys cached from the tenant's OpenID metadata) **before** any business logic runs. Reject all unauthenticated requests with 401.
4. For ARM and data-plane calls, the backend uses the **shared user-assigned Managed Identity** `id-elb-control` (mounted on the Container App and visible to all six sidecars) via `DefaultAzureCredential`. The bearer token is used only for identity verification (who is calling), not for downstream Azure calls. This avoids OBO consent issues and removes the need for `API_CLIENT_SECRET`.
5. The MI must be pre-granted sufficient RBAC roles (see [docs/auth.md](../auth.md) §1 for the full matrix). Runtime role assignments (e.g. granting AcrPull to AKS kubelet) are best-effort — if the MI lacks `User Access Administrator`, the code logs a one-line `az role assignment create` recovery hint instead of failing.
6. The `terminal` sidecar never holds a long-lived Azure credential. The user runs `az login --use-device-code` *inside the browser terminal session* the first time they connect. The resulting `~/.azure/` profile is persisted on the `terminal-home` Azure Files share so subsequent revisions keep the login.

> **Design choice**: We intentionally use Managed Identity instead of OBO. OBO requires `API_CLIENT_SECRET` and multi-resource consent, which are fragile in single-tenant research environments. MI simplifies deployment at the cost of the MI needing broad permissions — acceptable because the MI is scoped to the Container App and auditable via Azure Monitor.
