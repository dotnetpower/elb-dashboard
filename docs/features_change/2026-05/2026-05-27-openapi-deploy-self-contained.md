# Make the `elb-openapi` deploy self-contained + hide unreachable Swagger UI link

## Motivation
Two gaps surfaced after a fresh dashboard deploy:

1. **Deployed pod returned `503 "API authentication is not configured. Set
   ELB_OPENAPI_API_TOKEN..."`** on every `/v1/*` Try-It call.
   `api.tasks.openapi.deploy_openapi_service` only wrote
   `ELB_OPENAPI_API_TOKEN` into the deployment manifest **when the api
   sidecar already had one** (env var or runtime cache). On the first
   deploy both are empty, so `build_manifests` skipped the token env
   entry entirely and the sibling [`elb-openapi` `require_api_token`](https://github.com/dotnetpower/elastic-blast-azure/blob/main/docker-openapi/app/main.py)
   fail-closed branch returned 503 until the operator opened the API
   menu and clicked **Generate**. That post-deploy step was undocumented
   and easy to miss.
2. **"Swagger UI" link in the API Reference header pointed at the AKS
   internal LoadBalancer IP** (`http://10.224.0.7/docs`). The browser
   cannot reach an RFC1918 host from the public dashboard origin, and
   even if it could, that would be a plain-HTTP top-level navigation
   from an HTTPS page. The SPA's own API Reference is the intended
   surface in that case.

## User-facing change
- Running **Deploy elb-openapi** is now self-contained: when no token is
  cached anywhere, the deploy task mints a fresh `secrets.token_urlsafe(32)`
  token, writes it into the deployment manifest, persists it to the
  runtime cache, and updates the api sidecar process env. `/v1/*`
  Try-It works immediately after the rollout completes — no manual
  Generate click required.
- The API menu's **Generate / Refresh** flow still works for rotation
  (now its only purpose). The success payload from the deploy task gains
  `openapi_deploy.api_token_source` (`env` | `runtime_cache` |
  `auto_generated`) so the audit log records which path was taken.
- The "Swagger UI" button in the API Reference header is hidden when
  the resolved service `baseUrl` is a private / loopback / link-local
  IPv4 host. The SPA's API Reference page already proxies every call
  through the api sidecar, so operators do not lose any functionality.

## API / IaC diff summary
| Layer | File | Change |
|---|---|---|
| Tasks | [api/tasks/openapi/deploy.py](../../../api/tasks/openapi/deploy.py) | New token-resolution block: env → runtime cache → mint. Mint path persists the token via `save_openapi_api_token` and updates `os.environ`. `openapi_deploy.api_token_source` added to the success payload. |
| Frontend | [web/src/pages/apiReference/ApiHero.tsx](../../../web/src/pages/apiReference/ApiHero.tsx) | Added `isReachableUpstream(baseUrl)` guard around the `<a href="${baseUrl}/docs">` Swagger UI link. RFC1918 / 127.0.0.0/8 / 169.254.0.0/16 / 172.16.0.0/12 / 192.168.0.0/16 hosts are treated as unreachable. |

No new dependency. No IaC change. No new env var.

## HTTPS / transport posture (unchanged)
- The api sidecar → `elb-openapi` Service hop stays plain-HTTP inside
  the AKS VNet, gated by `_is_private_ipv4` (and the existing
  `OPENAPI_ALLOW_PUBLIC_LB` opt-in). No change here.
- The Container App's public ingress remains HTTPS-only; SPA → api
  hops are unchanged.
- The minted API token never leaves the server side: the manifest
  carries it to AKS over the kube-apiserver TLS channel, the runtime
  cache write goes to ops Redis on the loopback, and the SPA only
  receives the token after an authenticated GET/POST to
  `/api/aks/openapi/token` (the same path as before).

## Validation evidence
- `uv run ruff check api/tasks/openapi/deploy.py` → clean.
- `uv run pytest -q api/tests` → 1558 passed.
- `cd web && npx tsc -p tsconfig.json --noEmit` → clean.
- `cd web && npx eslint src/pages/apiReference/ApiHero.tsx` → clean.
