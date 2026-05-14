# 2026-05-15 — Auth claims cache, AGENTS.md navigation map, CAF resource tags

**Scope**: `api/auth.py`, `api/services/__init__.py`,
`api/tests/test_auth_caching.py` (new), `AGENTS.md` (new),
`infra/main.bicep`, `infra/modules/*.bicep`,
`.github/copilot-instructions.md`, `README.md`,
`api/routes/terminal_ws.py` (off-by-one fix).

## Motivation

Three independent productivity wins, grouped for one commit:

1. **Auth hot-path** — every `/api/*` route ran full RSA signature
   verification + claim validation per request. With the dashboard
   polling 5+ cards every 30 s, that was 10+ redundant validations per
   minute per session. Plus `AUTH_DEV_BYPASS=true` was advertised in
   docs / Dockerfile / tasks.json but unimplemented in code.
2. **Agent-facing scaffolding** — every fresh AI session re-discovered
   the same facts (api_app→api rename, retired Functions tree, no
   `pip install`, route order, SAS ban, ttyd loopback). A single
   navigation file at the standard `AGENTS.md` location amortises that
   cost.
3. **Resource tagging** — Bicep had only `azd-env-name` + `costCenter`,
   making cost analysis / ownership traces blind to component role.

## User-facing change

- Local dev: `AUTH_DEV_BYPASS=true` now actually short-circuits MSAL
  validation (returns synthetic identity), so `uv run uvicorn …` Just
  Works without a real bearer token.
- Steady-state `/api/*` routes are noticeably faster on the hot path
  (claims cached for ≤ 5 min by SHA-256 of the token).

## API / IaC diff summary

### `api/auth.py` — claims cache

- `_CLAIMS_CACHE: dict[sha256(token) -> (expires_at, CallerIdentity)]`.
  TTL = `min(JWT exp - 30 s skew, 300 s hard cap)` so a revoked token
  cannot live in our cache for more than 5 min.
- `threading.Lock` guard for multi-worker uvicorn safety.
- Soft cap 1024 entries; opportunistic expired-eviction then
  drop-soonest if still over (defends against fuzz-token expansion).
- SHA-256 cache key — raw token never persisted in the dict.
- Refresh is automatic: SPA acquires a new token → SHA differs → cache
  miss → re-validation. `reset_caches()` is test-only.
- `AUTH_DEV_BYPASS=true` short-circuits `require_caller`, returns
  synthetic `CallerIdentity(oid="00000000-…", upn="dev-bypass@local",
  raw_token="")`. Empty `raw_token` makes any downstream auth attempt
  fail loudly so the bypass never silently leaks into ARM calls.

### `api/services/__init__.py` — credential singleton

`get_credential()` now returns a module-level
`DefaultAzureCredential` singleton (was: new instance per call).
`DefaultAzureCredential` does its own internal token caching across the
chain, so creating multiple instances wasted both the instantiation
cost and the per-instance token caches. Matches the pattern already in
`azure_clients._get_mi_credential`. `reset_credential()` is test-only.

### `AGENTS.md` (new, ~190 lines, root)

Standard convention recognised by Claude / Cursor / Aider. Sections:

- TL;DR for fresh session (4 facts: active = `api/`, single Container
  App, `uv` workflow, no `pip install`).
- Where-to-read-first task table with file:line anchors.
- Backend route map (real vs 503/410 stubs; order constraint vs
  `frontend_proxy.router`).
- Backend / frontend / infra module maps.
- 9 trip-wire common mistakes (azure.functions import, bare `services.`
  imports, SAS reissue, requirements.txt, route order, legacy/ edits,
  Storage public-access toggle, ttyd binding, Run Command).
- Validation cheatsheet.

Cross-pointers added in `.github/copilot-instructions.md` §15 and
`README.md` header.

### Bicep tagging — CAF-aligned

`infra/main.bicep`:

- New `costCenter` (default `elasticblast`) + `ownerEmail` (optional)
  azd parameters.
- `var tags` expanded from {azd-env-name, costCenter, topology} to 7
  common keys: `azd-env-name`, `app=elb-dashboard`, `environment`,
  `costCenter`, `managedBy=azd`, `repo=https://…`,
  `topology=container-app-bundle-v1`. Conditional `owner` added when
  `ownerEmail` non-empty.

Each module (`infra/modules/*.bicep`):

- New `var moduleTags = union(tags, { role: '<…>' })` and `tags: tags`
  → `tags: moduleTags` everywhere. Roles: `acr→registry`,
  `containerAppControl→control-plane`,
  `containerAppsEnvironment→control-plane-env`, `identity→identity`,
  `keyvault→secrets`, `monitoring→observability`, `network→network`,
  `storage→platform-storage`. `storageState.bicep` has no tags param —
  child Storage resources inherit the account tags.

`copilot-instructions.md` §12 codifies the tag contract for new
modules.

### `api/routes/terminal_ws.py` — off-by-one ticket expiry

`expires_at < now` → `expires_at <= now` in two places (issue + redeem
sweep). Equals-now tickets must also be expired.

### Documentation refresh

- `.github/copilot-instructions.md`: Python 3.12 + uv-only policy block
  in §11; cross-pointer to AGENTS.md in §15; Storage Network Isolation
  hard requirement noted; Run Command ban + terminal_exec contract in
  §11.
- `README.md`: Prerequisites table refreshed (uv 0.9+, Python 3.12,
  azd 1.10+, drop Func Core); Local backend bring-up code block;
  Authentication / Roadmap / dashboard-preview retoned for the sidecar
  model.

## Validation

- `uv run pytest -q api/tests/test_auth_caching.py` → 9 passed
  (singleton reuse, cache hit, TTL eviction, 5-min hard cap, SHA-key
  non-leak, soft-cap eviction, dev-bypass identity, no-bypass still
  401).
- `uv run pytest -q api/tests` → **56 passed** total (no regressions).
- Hot-path benchmark (single-worker uvicorn, dev bypass on, /api/me ×
  1000, includes HTTP + RequestIdMiddleware): **2.2 ms / req**.
- `az bicep build infra/main.bicep` → exit 0; compiled ARM contains
  `union(parameters('tags'), createObject('role', '…'))` per module.
- `AGENTS.md` link checker: 32/32 relative links resolve.
