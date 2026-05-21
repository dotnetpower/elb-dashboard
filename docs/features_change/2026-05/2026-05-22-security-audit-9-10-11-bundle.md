# Security audit 2026-05-22 — items #9, #10, #11

## Motivation
Three small-radius HIGH/MEDIUM findings from the 2026-05-22 sweep,
bundled because each is a few lines in one file and they share the same
risk class (a misconfiguration or upstream behaviour that turns the api
sidecar into a privilege / data-leak vector).

- **#9 (HIGH)** — CORS configured from `CORS_ALLOW_ORIGINS` env var, no
  guard against `*` combined with the always-on `allow_credentials=True`.
  Browsers refuse the combination, but the server still emits the
  permissive header and is happy to read cookies on a downgraded
  non-credentialed flow — classic CSRF amplifier.
- **#10 (MEDIUM)** — `api/services/storage_public_access.py` called
  `https://api.ipify.org` on every Storage local-debug poll. A hard
  external dependency for a developer-only feature, and the dashboard's
  poll cadence (every few seconds for the Databases page) turned the
  helper into a steady probe stream — wasteful and a small side-channel.
- **#11 (HIGH)** — `api/routes/frontend_proxy.py` forwarded **every**
  header to the frontend nginx sidecar except hop-by-hop ones. That
  means the caller's MSAL bearer (`Authorization: Bearer …`) lands in
  the nginx access log on every static-asset fetch and is visible to
  any future middleware added to the frontend sidecar.

## User-facing change
- **#9** — Setting `CORS_ALLOW_ORIGINS='*'` now **crashes at app boot**
  with a clear `RuntimeError`. Same posture for `null` (sandboxed iframe
  / `data:` / `file:` origin) and any entry that does not parse as a
  `scheme://host` string. Listing concrete trusted origins continues to
  work exactly as before.
- **#10** — Caller-IP lookup is now cached for 10 minutes on success and
  30 seconds on failure, tries two providers in order, and honours an
  `ELB_LOCAL_CALLER_IP` env override so an offline laptop / CI runner
  never hits the network. Provider URLs are HTTPS-only — a typed
  `http://…` entry crashes the module at import time.
- **#11** — Frontend proxy now strips `Authorization`, `Cookie`,
  `X-ELB-API-Token`, and the four `X-Forwarded-Authorization` /
  `X-Forwarded-User` / `X-Forwarded-Access-Token` / `X-Forwarded-Id-Token`
  variants before forwarding to the nginx sidecar. Unrelated headers
  (`Accept`, `Accept-Encoding`, …) still pass through.

## API / IaC diff summary
| Layer | File | Change |
|---|---|---|
| App boot | [api/main.py](../../../api/main.py) | CORS env-parsing now rejects `*` / `null` / scheme-less entries at `create_app()`. |
| Routes | [api/routes/frontend_proxy.py](../../../api/routes/frontend_proxy.py) | New `_FRONTEND_STRIP_HEADERS` set; outbound header filter checks both `_HOP_BY_HOP` and the strip set. |
| Services | [api/services/storage_public_access.py](../../../api/services/storage_public_access.py) | New `_detect_caller_ip` helper with per-entry TTL cache (success / failure), two-provider fallback, `ELB_LOCAL_CALLER_IP` override, HTTPS-only provider enforcement at module import. |
| Tests | [api/tests/test_security_audit_bundle.py](../../../api/tests/test_security_audit_bundle.py) | New 9-test file: CORS wildcard refuses boot, explicit origins still work, CORS disabled when env empty, `null` refused, scheme-less refused, frontend strips `Authorization`, frontend strips `Cookie` + `X-ELB-API-Token`, frontend strips `X-Forwarded-*` variants, caller-IP HTTPS-only enforcement. |
| Tests | [api/tests/test_storage_public_access.py](../../../api/tests/test_storage_public_access.py) | Existing file extended in-place: cache reset fixture, env-override happy path, env-override garbage rejection, cache hit reuses first lookup, fallback to second provider when first 503s. |

No IaC changes. No new dependencies. No deploy required.

## Validation evidence
- `uv run ruff check api/main.py api/routes/frontend_proxy.py api/services/storage_public_access.py api/tests/test_smoke.py api/tests/test_storage_public_access.py api/tests/test_security_audit_bundle.py` → passed.
- `uv run pytest -q api/tests/test_security_audit_bundle.py` — **9 passed**.
- `uv run pytest -q api/tests` — **924 passed** (was 901 → +23 from bundle + cache tests).

## Hardening pass (same day)
A self-critique surfaced three additional weaknesses; fixed in the same
change:

- **CRITICAL — CORS `null` origin allowed.** The first draft only
  rejected `*`. The literal string `null` is the origin browsers send
  for sandboxed iframes, `data:` URLs, and `file:` contexts. Combined
  with `allow_credentials=True` it is a CSRF surface as bad as the
  wildcard. Fixed: explicit reject + regression test.
- **HIGH — Scheme-less CORS entry silently disabled CORS for the
  intended origin.** `CORS_ALLOW_ORIGINS=localhost:8090` (a real,
  observed typo) parsed as an opaque token that no browser would ever
  match against a real `Origin` header. Fixed: every entry must contain
  `://` and must not end with `://` (catches `localhost://`). Boot
  fails loudly with the offending entry quoted.
- **HIGH — `X-Forwarded-Authorization` family was not stripped.**
  Several ingress controllers (NGINX Ingress with the auth-request
  module, Azure App Gateway in some configs) propagate auth context
  via these headers. The first draft only stripped `Authorization`,
  `Cookie`, and `X-ELB-API-Token`. Fixed: added `x-forwarded-authorization`,
  `x-forwarded-user`, `x-forwarded-access-token`, `x-forwarded-id-token`
  to `_FRONTEND_STRIP_HEADERS`.
- **MEDIUM — Caller-IP provider could be plain HTTP.** A typo or a
  copy-paste from a forum post would route the discovery call through
  plaintext where a network attacker could supply a forged IP. The
  value is only informational today, but the helper now fails loudly
  at import time on any non-HTTPS provider.

New regression tests cover each vector so a future refactor cannot
quietly remove the guard.

## Non-goals (deferred)
- Stronger CORS validation (parse with `urllib.parse.urlparse` and reject
  query strings / fragments) — defer until we see a real misconfig.
- ECS-task-style IMDSv2 caller-IP lookup (`http://169.254.169.254/...`)
  for the in-cluster path — not needed; the helper has the `CONTAINER_APP_NAME`
  guard that prevents it from running inside the deployed Container App
  at all.
