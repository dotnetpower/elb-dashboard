# 2026-05-27 — API Reference: Copy as curl button

## Motivation

OpenAPI Try-It panel ([web/src/pages/apiReference/EndpointCard.tsx](../../../web/src/pages/apiReference/EndpointCard.tsx))
had a "Copy" button on the response viewer that copies the response JSON, but
no way to copy the *request* as a `curl` command. Researchers reproducing an
issue out of band (Slack thread, shell troubleshooting, doc snippet) had to
hand-reconstruct method + URL + body, which is error-prone for the proxy form
(`/api/aks/openapi/proxy?subscription_id=…&path=…`).

`ApiReference.tsx` and `apiReference/spec.ts` had load-bearing comments
mentioning a "future Copy curl affordance" — this lands that feature.

## User-facing change

- A new **Copy curl** button appears next to **Send Request** in every
  endpoint's *Try it* panel.
- The copied command:
  - Matches what the executor would actually send (direct vs proxy mode,
    path/query encoding, request body).
  - In **proxy mode** the dashboard's live MSAL bearer token is **inlined**
    so the command is immediately runnable (`curl … -H 'Authorization: Bearer
    <real-jwt>'`). Tooltip warns "includes a live bearer token — handle as a
    secret". If MSAL is unavailable (e.g. not signed in, dev-bypass), the
    command falls back to the `$AAD_TOKEN` placeholder so it stays useful as
    a template.
  - In **direct mode** no Authorization header is added (the upstream has
    its own auth posture).
  - Quotes the body with POSIX-safe single-quotes (`'\''`).
- Toast feedback reuses the existing `useClipboardFeedback` label
  `openapi-curl`.

## API / IaC diff

None. Pure frontend.

- [web/src/api/client.ts](../../../web/src/api/client.ts) — exports new
  `getApiAccessToken()` helper (thin wrapper over the existing internal
  `getAccessToken()`).
- [web/src/hooks/useOpenApiExecutor.ts](../../../web/src/hooks/useOpenApiExecutor.ts) — adds exported `buildCurl()` (now accepts optional `bearerToken`) + async `copyCurl` callback that fetches the live token.
- [web/src/hooks/useOpenApiExecutor.test.ts](../../../web/src/hooks/useOpenApiExecutor.test.ts) — 6 new unit tests (curl direct, proxy with placeholder, proxy with inlined token, fallback to placeholder when token is null/empty, single-quote escape, apiBase precedence).
- [web/src/pages/apiReference/EndpointCard.tsx](../../../web/src/pages/apiReference/EndpointCard.tsx) — wires button into Try-It header with secret-handling warning tooltip.
- [web/src/pages/ApiReference.tsx](../../../web/src/pages/ApiReference.tsx) — comment updated (no longer "future").

## Validation

- `cd web && npx vitest run src/hooks/useOpenApiExecutor.test.ts` → 14/14 passed.
- `cd web && npm test -- --run` → 383/383 passed.
- `cd web && npm run build` → built in 7.82s, no new warnings.
- Manual smoke from API Reference page pending (UI bring-up); button is
  rendered identically across proxy and direct mode by the existing
  Try-It path.

## Security notes

- `buildCurl()` accepts an optional `bearerToken`. When the hook is invoked
  through `copyCurl()` in proxy mode it **inlines the live MSAL access token**
  so the copied command runs as the user without further editing. This is an
  intentional UX trade-off requested by the user; the tooltip warns explicitly
  ("includes a live bearer token — handle as a secret").
- The token is the same short-lived (~1 h) bearer the SPA already attaches to
  every `/api/*` call. It carries no broader privilege than the running
  session.
- When MSAL is unavailable (not signed in, dev-bypass mode), the placeholder
  `$AAD_TOKEN` is emitted instead — copying the command never throws.
- No SAS tokens, subscription IDs, or other credentials are inlined. The
  proxy URL itself does contain subscription / RG / cluster name (it already
  does in normal Try-It traffic).
