# Security audit 2026-05-22 — items #12, #13, #14, #18, #20

## Motivation
The final small-radius bundle from the 2026-05-22 sweep. Each item is a
contained fix that closes a "trust caller / upstream more than necessary"
gap or removes a centralisation gap that could lead to inconsistency:

- **#12 (HIGH)** — `/api/aks/openapi/proxy` auto-injects the admin
  `X-ELB-API-Token` and forwards over plain HTTP to whatever IP
  `k8s_get_service_ip` returns. If the operator wires `elb-openapi` as
  a public LoadBalancer, the admin token travels over the open
  internet between the api sidecar and the public LB.
- **#13 (MEDIUM)** — `ensure_local_storage_access` flips
  `defaultAction=Allow` (intentional, see service docstring) but relies
  on `allowSharedKeyAccess=false` to keep AAD-only data-plane auth.
  The invariant was assumed but never checked at flip time.
- **#14 (MEDIUM)** — `POST /api/arm/resource-group/tags` checked only
  the `elb-` prefix on tag names. Tag name length (Azure cap: 512),
  value length (cap: 256), tag count (cap: 50), and the set of
  characters Azure rejects in tag names (`<>%&\?/` + control chars)
  were unchecked, leaving the api to leak raw SDK exceptions back to
  the SPA.
- **#18 (MEDIUM)** — The frontend catch-all proxy accepted any
  method / path. Path-traversal segments, control characters
  (CR/LF / NUL), and exotic methods (TRACE / CONNECT) were forwarded
  to the nginx sidecar without sanitisation.
- **#20 (MEDIUM)** — `https://{account}.blob.core.windows.net` was
  hardcoded in 7 call sites (services + routes). A sovereign-cloud
  deployment (US Gov, China) would have to chase every occurrence.

## User-facing change
- **#12** — `GET|POST /api/aks/openapi/proxy?...` returns
  `502 {"code": "openapi_unsafe_transport", "message": "..."}` when
  the resolved upstream IP is not in an RFC1918 / loopback / link-local
  range. The admin token is **never** emitted in the response body.
- **#13** — `POST /api/storage/local-debug/open` (and the implicit
  auto-open path) returns
  `{"action": "failed", "error": "shared_key_access_enabled", ...}`
  when the storage account has `allowSharedKeyAccess=true`. The
  operator must set the property to `false` (via Bicep or `az`) before
  opening the network window.
- **#14** — Tag posts now return `400` with a clear message when:
  - tag count > 50
  - tag name > 512 chars or contains `<>%&\?/` / control chars
  - tag value > 256 chars or contains control chars
- **#18** — Catch-all frontend route returns `400 path contains
  parent-traversal segment` or `400 path contains control characters`
  for the matching probes, and `405 method not allowed for frontend
  assets` for anything outside `{GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS}`.
- **#20** — New `api/services/storage_endpoint.py` helper module
  (`blob_account_url`, `table_account_url`, `dfs_account_url`,
  `blob_host_for_account`, `azure_storage_suffix`). All 7 production
  call sites now route through it. Sovereign-cloud deployment is a
  one-env-var change: `AZURE_STORAGE_SUFFIX=core.usgovcloudapi.net`.

## API / IaC diff summary
| Layer | File | Change |
|---|---|---|
| Routes | [api/routes/aks/openapi.py](../../../api/routes/aks/openapi.py) | New `_is_private_ipv4` helper + gate before token injection in `aks_openapi_proxy`. |
| Routes | [api/routes/arm.py](../../../api/routes/arm.py) | New `_validate_tag_name`, `_validate_tag_value` helpers + count cap; wired into `set_rg_tags`. |
| Routes | [api/routes/frontend_proxy.py](../../../api/routes/frontend_proxy.py) | New `_FRONTEND_ALLOWED_METHODS` set + traversal/control-char/method guards before forwarding. |
| Services | [api/services/storage_public_access.py](../../../api/services/storage_public_access.py) | `ensure_local_storage_access` now reads `allow_shared_key_access` and refuses to open the window when True. |
| Services (new) | [api/services/storage_endpoint.py](../../../api/services/storage_endpoint.py) | Centralised Storage endpoint helper module. |
| Services (migrated) | [api/services/storage_data.py](../../../api/services/storage_data.py), [api/services/job_artifacts.py](../../../api/services/job_artifacts.py), [api/services/storage_url_validation.py](../../../api/services/storage_url_validation.py), [api/services/db_sharding.py](../../../api/services/db_sharding.py), [api/services/blast_oracles.py](../../../api/services/blast_oracles.py), [api/services/blast/task_config.py](../../../api/services/blast/task_config.py), [api/services/db_order_oracle.py](../../../api/services/db_order_oracle.py), [api/routes/storage/prepare_db.py](../../../api/routes/storage/prepare_db.py) | All hardcoded `f"https://{account}.blob.core.windows.net"` strings replaced with `blob_account_url(account)` / `blob_host_for_account(account)`. |
| Tests | [api/tests/test_security_audit_12_13_14_18_20.py](../../../api/tests/test_security_audit_12_13_14_18_20.py) | New 14-test file: ARM tag limits × 4, frontend path/method guards × 3, Storage shared-key refusal × 2, endpoint helper × 5. |
| Tests | [api/tests/test_openapi_proxy_route.py](../../../api/tests/test_openapi_proxy_route.py) | Switched stub Service IP from `20.30.40.50` (public) to `10.0.0.50` (RFC1918) so existing happy-path tests reflect the real deployment; added 3 new tests for public-IP refusal + IPv6 conservative-default refusal × 2. |

No IaC changes. No new dependencies. No deploy required.

## Validation evidence
- `uv run ruff check` on every touched file → passed.
- `uv run pytest -q api/tests/test_security_audit_12_13_14_18_20.py` — **14 passed**.
- `uv run pytest -q api/tests/test_openapi_proxy_route.py` — **22 passed**.
- `uv run pytest -q api/tests` — **961 passed** (was 943 → +18 from #12–#20 + IPv6 hardening).

## Hardening pass (same day)
A self-critique surfaced one additional weakness; fixed in the same change:

- **HIGH — IPv6 was silently accepted as "non-private" → refused.** The
  first draft of `_is_private_ipv4` only handled IPv4 via
  `IPv4Address(value)` and fell through to `False` for IPv6, which the
  proxy interprets as "non-private → refuse". That happens to be the
  conservative default (refuse on IPv6 too), but a future operator
  enabling dual-stack AKS would be confused by the 502 with no
  diagnostic. The docstring now states the limitation explicitly and
  the test file pins both IPv6 cases (private ULA `fd00::/8` and
  globally-routable Cloudflare DNS) to the same 502 so a future fix is
  forced to update the URL builder and this check together.

## Non-goals (explicitly deferred)
- **#15 (token cache TTL)** — Reducing the 5-min cap is a security /
  UX trade-off (faster revocation propagation vs. JWKS call rate). Wants
  user input.
- **#16 (`az` allowlist)** — `az` is itself a script interpreter; the
  fix is either (a) remove `az` from the exec_server allowlist or
  (b) document that `az` requires the same trust level as the user.
  Needs design discussion.
- **#17 (HTTP rate limit)** — Adds a new dependency (`slowapi` or
  similar) and requires user input on the rate ceiling.
- **#19 (execToken rotation tracking)** — IaC + Container App secret
  rotation pipeline.
- **#1 / #2** — Already covered by
  [docs/copilot/security-audit-followup.md](../../copilot/security-audit-followup.md)
  design doc; both require larger sprints (App Role / per-ticket tmux).

## Audit progress
14/20 audit findings now closed (#3, #4, #5, #6, #7, #8, #9, #10, #11,
#12, #13, #14, #18, #20). Remaining: #1, #2, #15, #16, #17, #19.
