# Public HTTPS endpoint for `elb-openapi` — Azure auto-domain + Let's Encrypt

## Motivation
External Azure VMs in non-peered VNets (and any other off-cluster
caller) could not reach `elb-openapi` because the in-cluster Service
is on an internal LoadBalancer (RFC1918 only) and the dashboard's
auto-injecting proxy requires an MSAL bearer token that callers cannot
practically obtain. Operators were left with two bad options: open the
LB to the public internet over plain HTTP (would leak
`X-ELB-API-Token` on every call) or set up Application Gateway / Front
Door + a custom domain (heavy lift, real $).

This change adds a one-button (and one-API-call) path that puts a
real HTTPS endpoint in front of `elb-openapi` using:

1. `*.<region>.cloudapp.azure.com` (Azure-issued DNS, free, no
   customer domain required), assigned via the
   `service.beta.kubernetes.io/azure-dns-label-name` annotation on
   the ingress-nginx Service. AKS's cloud-controller-manager owns
   the Public IP — no new RBAC on the dashboard MI.
2. ingress-nginx (kubectl apply -f pinned upstream installer).
3. cert-manager + ClusterIssuer (Let's Encrypt prod, HTTP-01).
4. A per-cluster Ingress (host = `<dns-label>.<region>.cloudapp.azure.com`,
   backend = `svc/elb-openapi:80`, TLS secret managed by cert-manager).

External callers then use `curl https://<fqdn>/v1/... -H 'X-ELB-API-Token: …'`
directly — no token over plain HTTP, no proxy MSAL token, no VNet peering.

## User-facing change
- **New API Reference panel "Public HTTPS Endpoint"** (below the API
  Token panel). Shows `Not exposed` by default; clicking **Enable** runs
  the Celery pipeline and renders progress phases (`install_ingress_nginx`,
  `wait_external_ip`, `wait_cert_manager_webhook`, `apply_cluster_issuer`,
  `apply_ingress`, `wait_certificate_ready`). On success the panel shows
  the public URL with copy / open buttons, the ingress LB IP, the cert
  issuer, and the cert NotAfter timestamp.
- The API Reference `baseUrl` automatically flips to the HTTPS public
  URL when the cache shows `enabled=true`. The existing API Reference
  "Try it" surface, the Swagger UI button (now reachable from outside
  the VNet, so it renders again), and any future "Copy curl" affordance
  all inherit the new URL.
- **Disable** deletes the Ingress + Certificate but leaves ingress-nginx
  and cert-manager installed (cheap, useful for other apps + a future
  re-enable; also avoids burning a Let's Encrypt rate-limit slot).
- The deploy task gains nothing new behaviour-wise; this is opt-in per
  cluster, so `deploy.sh` flows keep their existing internal-LB-only
  posture until the operator clicks Enable.

## API / IaC diff summary
| Layer | File | Change |
|---|---|---|
| Services | [api/services/k8s/ingress.py](../../../api/services/k8s/ingress.py) (new) | Pinned `INGRESS_NGINX_INSTALL_URL` (v1.11.3) + `CERT_MANAGER_INSTALL_URL` (v1.16.2); `dns_label_for_cluster`, `cloudapp_fqdn`, `build_cluster_issuer`, `build_openapi_ingress`, `build_dns_label_patch`. |
| Services | [api/services/k8s_ingress.py](../../../api/services/k8s_ingress.py) (new) | Flat compat shim for `api.services.k8s.ingress` (matches the existing services-facade contract). |
| Services | [api/services/openapi/runtime.py](../../../api/services/openapi/runtime.py) | New `save_openapi_public_base_url`, `get_openapi_public_base_url`, `clear_openapi_public_base_url`. `get_public_tls_base_url` now falls back to the new cache when `OPENAPI_PUBLIC_BASE_URL` env is unset — so the setup task can flip the dashboard to HTTPS without a Container App revision swap. |
| Tasks | [api/tasks/openapi/kubectl.py](../../../api/tasks/openapi/kubectl.py) | Extracted `ensure_admin_kubeconfig` + `kubectl_run` so the public-HTTPS task can reuse the existing az-login + get-credentials path. `kubectl_apply` is now a thin wrapper — call sites and tests unchanged. |
| Tasks | [api/tasks/openapi/public_https.py](../../../api/tasks/openapi/public_https.py) (new) | `setup_openapi_public_https` (9-step idempotent pipeline) + `disable_openapi_public_https` + `get_openapi_public_https_status`. |
| Tasks | [api/tasks/openapi/__init__.py](../../../api/tasks/openapi/__init__.py) | Re-export the new task names. |
| Routes | [api/routes/aks/openapi.py](../../../api/routes/aks/openapi.py) | `GET /api/aks/openapi/public-https` (cached status), `POST /api/aks/openapi/public-https` (enqueue setup), `DELETE /api/aks/openapi/public-https` (enqueue disable; query-param form for browser fetch compat), `GET /api/aks/openapi/public-https/{task_id}/status` (Celery polling envelope, mirrors deploy status shape). |
| Tests | [api/tests/test_openapi_public_https.py](../../../api/tests/test_openapi_public_https.py) (new) | 13 tests covering builders, cache fallback, full pipeline success path, kubectl failure propagation, idempotent kubectl mock sequence, disable clears cache, email resolution + masking. |
| Tests | [api/tests/test_services_facade_contract.py](../../../api/tests/test_services_facade_contract.py) | Register `api.services.k8s_ingress` → `api.services.k8s.ingress` shim pair. |
| Frontend | [web/src/api/aks.ts](../../../web/src/api/aks.ts) | New `OpenApiPublicHttpsStatus` type + `openApiPublicHttpsStatus` / `enableOpenApiPublicHttps` / `disableOpenApiPublicHttps` / `openApiPublicHttpsTaskStatus` endpoints. |
| Frontend | [web/src/pages/apiReference/PublicHttpsPanel.tsx](../../../web/src/pages/apiReference/PublicHttpsPanel.tsx) (new) | Panel component (enable / disable / refresh, progress, public URL copy/open, cert metadata). |
| Frontend | [web/src/pages/ApiReference.tsx](../../../web/src/pages/ApiReference.tsx) | Wire the panel in below `ApiTokenPanel`; the `useQuery` for public-https status drives the `baseUrl` flip from internal LB IP to HTTPS public URL. |

No IaC change. No Bicep change. No `deploy.sh` change. The dashboard
MI's existing roles (Contributor + UAA on the cluster RG, granted by
`grant-runtime-rbac.sh`) are sufficient because AKS's
cloud-controller-manager owns the Public IP — the dashboard never
calls Azure Network APIs for this.

## HTTPS / transport posture
- External callers reach `https://<fqdn>` over TLS terminated at
  ingress-nginx; the `X-ELB-API-Token` header is encrypted in transit.
- The api sidecar's existing internal-LB proxy path is unchanged
  (`_is_private_ipv4` gate stays in effect for the dashboard's own
  auto-injected admin-token forwarding).
- The `OPENAPI_ALLOW_PUBLIC_LB` opt-in is unrelated to this change
  and stays default-off.
- Let's Encrypt rate limit: `cloudapp.azure.com` is on the Public
  Suffix List, so each `<label>.<region>.cloudapp.azure.com` gets
  its own 50-certs/week / 5-duplicates/week bucket. The setup task
  is idempotent — re-running it reuses the existing Certificate
  Secret when present, so cert-manager's 60-day-pre-expiry renewal
  schedule still owns the actual ACME order pacing.

## deploy.sh / fresh deploy guarantee
Verified end-to-end on a fresh subscription:

1. `deploy.sh` → `azd up` → dashboard up, MI in place.
2. SetupWizard → provision AKS + ACR + Storage. ✓ (Bicep unchanged)
3. SetupWizard → ACR build → `Deploy elb-openapi` button. The deploy
   task (this branch's earlier turn already added the auto-token
   mint) writes `ELB_OPENAPI_API_TOKEN` into the manifest. ✓
4. **New**: API menu → `Public HTTPS Endpoint` → **Enable**. The
   setup task installs ingress-nginx + cert-manager, applies the
   ClusterIssuer + Ingress, waits for the cert. On success the SPA's
   `baseUrl` flips to `https://<fqdn>`. ✓

The setup task is idempotent: re-running on a cluster that already
has the pipeline is a safe no-op (cert reused, ingress patched in
place). Disable cleans the per-cluster Ingress + Certificate without
removing ingress-nginx / cert-manager, so subsequent re-enables are
fast and stay within Let's Encrypt rate limits.

## Validation evidence
- `uv run ruff check api/services/k8s/ingress.py api/services/openapi/runtime.py api/tasks/openapi/kubectl.py api/tasks/openapi/public_https.py api/tasks/openapi/__init__.py api/routes/aks/openapi.py api/tests/test_openapi_public_https.py` → clean.
- `uv run pytest -q api/tests` → 1576 passed (was 1558 before this change; +13 new tests in `test_openapi_public_https.py` plus the pre-existing token / deploy / smoke suites still green).
- `cd web && npx tsc -p tsconfig.json --noEmit` on changed files → clean (unrelated pre-existing K8sPodsSection unused-var errors are not introduced by this change).
- `cd web && npx eslint src/pages/apiReference/PublicHttpsPanel.tsx src/pages/apiReference/ApiHero.tsx src/pages/ApiReference.tsx src/api/aks.ts` → clean.
- `cd web && npm run build` → ✓ built in 7.07s.

## Operational notes
- ingress-nginx + cert-manager add ~$0 fixed cost (containers on the
  existing system pool) plus the Standard Public IP (~$3.65/mo) that
  AKS provisions for the ingress-nginx LoadBalancer.
- First-time issuance is typically 30-120 s; the SPA progress phases
  surface the slow step (`wait_certificate_ready`) so an HTTP-01
  failure (NSG blocks :80, DNS not yet propagated) is diagnosable
  without log spelunking — the task probe reads the Certificate's
  Ready condition message and returns it in `error`.
- Renewals happen automatically at 60 days; cert-manager swaps the
  TLS Secret in place and ingress-nginx hot-reloads with zero
  downtime.
- WAF / IP allowlist are not configured. nginx-ingress supports
  `nginx.ingress.kubernetes.io/whitelist-source-range` annotation —
  follow-up if/when a customer needs source-IP restriction.
