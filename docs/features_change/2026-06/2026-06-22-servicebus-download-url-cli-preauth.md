---
title: Service Bus result download_url — pre-authorize Azure CLI so the documented token path works
description: Fix AADSTS65001 that made completion-event download_url return 401 for the example consumer by pre-authorizing the Azure CLI public client on the API app registration.
tags:
  - blast
  - auth
---

# Service Bus result `download_url` — pre-authorize the Azure CLI

## Motivation

A Service Bus completion-event subscriber (the shipped example
`example/servicebus/consume.py --download`) reported that the
`result_files[].download_url` "does not actually download". The URL points at
the dashboard's authenticated streaming gateway
(`GET /api/v1/elastic-blast/jobs/{id}/files/{file_id}`) and requires a bearer
token with `aud = api://<API_CLIENT_ID>` — by design, never a SAS URL
(charter §9).

Root cause (reproduced live against the deployed API app registration):

- The documented example acquires that token via
  `az account get-access-token --resource <API_CLIENT_ID>`.
- The API app registration exposes only the `user_impersonation` delegated
  scope and has an **empty `preAuthorizedApplications` list**, so the well-known
  Azure CLI public client (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) is not
  pre-authorized.
- The non-interactive token request therefore fails with `AADSTS65001`
  ("the user or administrator has not consented"), the script gets an empty
  token, and the download endpoint returns `401 missing bearer token`.

This is an **app-registration configuration** gap, not a control-plane code
defect — the download route, `stream_file` token self-heal, and the Storage
fallback are all healthy (the endpoint returns a correct `401` without a token).
No container image change is involved, so this does not require a redeploy.

## User-facing change

- Fresh deployments configured by `scripts/dev/setup-app-registration.sh` now
  pre-authorize the Azure CLI public client for the `user_impersonation` scope,
  so `az account get-access-token --resource <api-client-id>` (and therefore the
  Service Bus result-download example) works without a per-user consent prompt.
- `consume.py` now prints an actionable hint when the token request fails with
  `AADSTS65001`, pointing at the pre-authorization requirement and the
  `ELB_BEARER_TOKEN` fallback.
- The example README and `docs/architecture/service-bus-examples.md` document
  the pre-authorization requirement and the 401 remediation.

## API / IaC diff summary

- `scripts/dev/setup-app-registration.sh`: new idempotent step (3b) that reads
  the current `api` object and PATCHes a merged `preAuthorizedApplications`
  entry for the Azure CLI app id, preserving the exposed scope and any existing
  pre-authorized apps. Summary block now reports the CLI pre-authorization.
- `example/servicebus/consume.py`: AADSTS65001-aware error guidance + docstring
  note.
- `example/servicebus/README.md`, `docs/architecture/service-bus-examples.md`:
  "download_url returns 401" remediation note.

No `api/` runtime code changed; no Bicep/Container App template changed.

## Applying to an existing deployment

The repo change covers new deployments. An already-created app registration is
fixed with a one-time, reversible Microsoft Graph PATCH (security note: this
only removes the consent prompt for a scope the SPA already uses; the backend
still validates every token via `require_caller`):

```bash
APP_OBJECT_ID="$(az ad app show --id <api-client-id> --query id -o tsv)"
SCOPE_ID="$(az ad app show --id <api-client-id> \
  --query "api.oauth2PermissionScopes[?value=='user_impersonation'].id | [0]" -o tsv)"
az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications/$APP_OBJECT_ID" \
  --headers "Content-Type=application/json" \
  --body "{\"api\":{\"preAuthorizedApplications\":[{\"appId\":\"04b07795-8ddb-461a-bbee-02f9e1bf7b46\",\"delegatedPermissionIds\":[\"$SCOPE_ID\"]}]}}"
```

(Re-running `scripts/dev/setup-app-registration.sh` does the same, idempotently,
while also preserving existing pre-authorized apps.)

## Validation evidence

- `bash -n scripts/dev/setup-app-registration.sh` — OK.
- jq merge simulation: preserves `oauth2PermissionScopes`, preserves an
  unrelated pre-authorized app, drops a stale Azure CLI entry, and re-adds the
  CLI with the current scope id (idempotent).
- `python3 -m py_compile example/servicebus/consume.py` + `consume.py
  --self-test` — OK.
- `scripts/docs/check_frontmatter.py` — OK (57 navigated pages).
- Live reproduction of the root cause: `az account get-access-token --resource
  <api-client-id>` returned `AADSTS65001`; the API app registration showed
  `preAuthorizedApplications: []` and only the `user_impersonation` scope; the
  unauthenticated download endpoint returned `401 missing bearer token`
  (endpoint healthy).
