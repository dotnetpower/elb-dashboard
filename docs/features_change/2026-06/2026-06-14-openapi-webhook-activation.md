---
title: "Activate elb-openapi terminal-transition webhook (F4)"
description: "Implement the dashboard receiver and AKS pod env wiring so the sibling elb-openapi pod's _webhook_notify accelerates jobstate freshness past the periodic /v1/jobs poll cycle."
tags: [blast, operate]
---

# Activate elb-openapi terminal-transition webhook (F4)

## Motivation

`elb-openapi:4.24` (sibling commit `5b2d6abd`) ships
`_notify_terminal_transition` / `_webhook_notify` that POSTs every
submit / terminal-state event to `{CONTROL_PLANE_URL}/api/blast/register-external-job`.
The dashboard side had neither the receiver route nor the AKS pod env vars
(`CONTROL_PLANE_URL`, `ELB_OPENAPI_INTERNAL_TOKEN`), so the feature was dormant
on 4.24 — the dashboard kept its previous behaviour of waiting for the next
`/v1/jobs` poll to learn a job finished, which adds up to ~30s of latency per
terminal transition.

This change wires F4 end-to-end so the next "Deploy elb-openapi" rollout
activates it.

## User-facing change

- BLAST jobs whose lifecycle is driven by the sibling pod flip to their final
  status (`completed` / `failed` / `cancelled`) in the dashboard as soon as the
  pod observes the transition, instead of after the next poll. Status badges,
  jobs list, and result-ready signals reflect the change immediately.
- No SPA UI change. The acceleration is invisible aside from the faster status
  refresh.

## API / IaC diff summary

- **New route** `POST /api/blast/register-external-job` (api/routes/blast/external_webhook.py):
  - Auth = static bearer; accepts `ELB_OPENAPI_INTERNAL_TOKEN` or falls back to
    `ELB_OPENAPI_API_TOKEN` (single shared cluster secret — splitting them would
    double secret-management cost with no real security gain at v1).
  - Body: `{job_id, event?, status?, error?}` with `extra="allow"` for forward
    compatibility.
  - Returns **202 on every auth-success path** (unknown job, state-repo
    unavailable, transient update failure all surface as
    `{synced:false, reason:...}` so the sibling never enters a retry storm).
  - **Forward-only state machine**: a `running` row that receives a `submitted`
    / `queued` event is ignored (`backward_transition_ignored`) so an
    out-of-order webhook cannot regress the dashboard view.
  - **Idempotent**: same-status re-delivery returns `{synced:true, noop:true}`
    with no jobstate write.
  - Returns **503 `webhook_not_configured`** when neither env token is set —
    this is intentionally distinct from 401 so App Insights makes it obvious
    the failure is on the dashboard side, not a sibling fault.
  - `include_in_schema=False` (not part of the public OpenAPI surface).
- **Manifest wiring** (api/tasks/openapi/manifests.py): `build_manifests`
  gained an optional `control_plane_url: str = ""` kwarg. When non-empty the
  AKS Deployment env list also carries `CONTROL_PLANE_URL` and
  `ELB_OPENAPI_INTERNAL_TOKEN` (= `api_token`). Omitting the kwarg preserves
  the previous 4-env layout so existing tests / local-dev paths are unchanged.
- **Deploy task** (api/tasks/openapi/deploy.py): new `_resolve_control_plane_url()`
  helper composes the dashboard's own public URL with precedence
  `DASHBOARD_PUBLIC_URL` env override → `https://${CONTAINER_APP_NAME}.${CONTAINER_APP_ENV_DNS_SUFFIX}`
  → `""`. Container Apps inject those two env vars into every revision
  automatically, so no Bicep change is required.
- **No infra change.** No new Bicep, no new secret, no new role assignment.

## Why not a separate secret?

The sibling already uses `ELB_OPENAPI_API_TOKEN` as its inbound auth secret;
reusing it as the dashboard's webhook-receiver bearer keeps the trust boundary
explicit ("one cluster, one secret"). The receiver still accepts a dedicated
`ELB_OPENAPI_INTERNAL_TOKEN` (the env var the sibling populates) so a future
rotation can split them without code changes.

## Self-critique safeguards

Applied during design (per `.github/skills/self-critique-review`):

- **Contract / state machine** — Forward-only writes prevent regression from
  an out-of-order webhook; idempotent same-status is a noop; terminal-success
  clears stale `error_code` so a previous transient failure doesn't linger.
- **Unbounded retry / wait loops** — No internal retry. The sibling already
  retries 3x with exponential backoff (1s/2s); the receiver returns 202 on
  every failure path so the sibling never escalates beyond that.
- **Idempotency** — Same-status re-delivery returns `{synced:true, noop:true}`
  without a jobstate write; verified by
  `test_register_external_job_idempotent_same_status`.
- **Concurrency / races** — The unknown-job branch deliberately does NOT create
  a jobstate row (the submitter owns row creation with the right `owner_oid`);
  the webhook only updates existing rows. This avoids a TOCTOU between submit
  and webhook arrival.
- **Partial failure** — `KeyError` on `update` (row deleted between get and
  update) → `row_gone`. Any other exception → `update_failed`. Both surface as
  202 so the sibling moves on.
- **Observability** — Every code path emits an INFO log line so App Insights
  can answer "did the webhook arrive?" and "what did the receiver decide?".
  Misconfiguration is a 503 (not silent), so the sibling's retry exhaustion
  shows up immediately.

## Validation evidence

- `uv run ruff check api` — All checks passed.
- `uv run pytest -q api/tests/test_external_webhook.py api/tests/test_openapi_task.py`
  → 33 passed in 4.38s (14 new external-webhook tests + 5 new manifests / deploy
  tests + the existing 14 openapi-task tests).
- `uv run pytest -q api/tests` (full backend suite) →
  **3546 passed, 3 skipped** in 14.28s.
- Live verification (post-deploy) — after triggering "Deploy elb-openapi" from
  the SPA, submit a BLAST job and confirm:
  - `kubectl --kubeconfig /tmp/kc logs -n elb deploy/elb-openapi --tail=200 | grep -i webhook`
    shows `Webhook sent for job ...`.
  - Container App api log shows `openapi webhook: job_id=...` INFO lines.
  - The jobstate row flips terminal status without waiting for the periodic
    `/v1/jobs` poll cycle.
