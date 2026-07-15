---
title: Feature gate registry
description: Single reference for the environment-variable feature gates that harden or relax the elb-dashboard control plane — each row lists the default state, the effect when enabled, where it is read, and whether it is a production hardening toggle, an escape hatch, or a local-debug-only switch.
tags:
  - operate
  - security
---

# Feature gate registry

This page is the single index of the environment-variable gates that change the
behaviour of the control plane. It exists so an operator can answer two
questions without grepping the codebase:

1. **What is the default?** Every gate below ships **default-OFF / legacy
   behaviour preserved** unless explicitly noted, per
   [charter §12a Rule 4](https://github.com/dotnetpower/elb-dashboard/blob/main/.github/copilot-instructions.md).
2. **Is it safe to flip?** Each row states whether the gate is a *production
   hardening* toggle (safe to enable after a soak window), an *escape hatch*
   (only for a specific known-safe situation), or a *local-debug-only* switch
   that must never reach a deployed Container App.

> Adding a new gate? Name it `STRICT_<area>` or `ENFORCE_<area>` (hardening) and
> register it here in the same change, with the planned flip date. That is the
> §12a Rule 4 contract.

## Production hardening gates (default-OFF, opt-in)

These follow the §12a Rule 4 lifecycle: ship default-OFF behind the env var,
soak for one release cycle with the gate forced ON in dogfood + a green
[Persona Matrix](https://github.com/dotnetpower/elb-dashboard/blob/main/api/tests/test_persona_matrix.py)
run, then flip the default in a separate PR.

| Gate | Default | Effect when `=true` | Read by |
| --- | --- | --- | --- |
| `STRICT_JWT` | off | Lowers the claims cache TTL from 300 s to 60 s and pins the token `azp`/audience on every validation. | [api/auth.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/auth.py) |
| `STRICT_CORS` | off | Locks the CORS allow-list to same-origin; `STRICT_CORS_ALLOW_METHODS` / `STRICT_CORS_ALLOW_HEADERS` (comma-separated) override the defaults for custom flows. | [api/main.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/main.py) |
| `STRICT_EXEC_RATE_LIMIT` | off | Enables a per-window rate limit on the loopback exec server in the `terminal` sidecar. Setting it back to `false` re-opens the gate immediately. | [terminal/exec_server.py](https://github.com/dotnetpower/elb-dashboard/blob/main/terminal/exec_server.py) |
| `STRICT_CSP` | off | Emits a strict `Content-Security-Policy` response header on API + proxied SPA responses (kept in sync with `web/nginx.conf`). `STRICT_CSP_POLICY` overrides the default policy string. | [api/app/security_headers.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/app/security_headers.py) |
| `STRICT_READINESS_DETAIL` | off | Collapses the `/api/health/ready` body to the overall status only (drops the per-component `components` map that leaks internal topology to an anonymous recon probe). Default OFF preserves the full-detail body the cli-upgrade Tier-1 gate reads. | [api/routes/health.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/routes/health.py) |
| `STRICT_SSE_TICKET_BINDING` | off | Binds the one-shot SSE ticket to the caller object id, client IP, and User-Agent hash, and rejects consumption when any differs (audit P0 #2 #3). | [api/routes/monitor/sidecars.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/routes/monitor/sidecars.py), [api/routes/monitor/logs.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/routes/monitor/logs.py) |
| `STRICT_AUDIT_HASH` | off | Redacts PII out of `jobhistory.payload_json` by hashing matched fields before the append-blob audit write (audit P2 #13 #14). | [api/services/state/repository.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/services/state/repository.py) |
| `STRICT_CLIENT_LOG_REDACTION` | off | Hashes the authenticated caller identifier and removes query/fragment data from browser error-report URLs before logging. Planned flip date: 2026-08-15, after one dogfood release with the gate forced ON and a green Persona Matrix run. | [api/routes/client_log.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/routes/client_log.py) |
| `ENFORCE_OPENAPI_EXEC_RBAC` | off (`false` in Bicep) | Requires the caller to hold an [Azure RBAC](https://learn.microsoft.com/azure/role-based-access-control/overview) write role on the target resource group before a state-changing OpenAPI proxy call is forwarded under the admin token. See [OpenAPI execution RBAC gate](openapi-exec-rbac-gate.md). | [api/services/openapi/exec_gate.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/services/openapi/exec_gate.py) |
| `ALLOW_OPENAPI_TOKEN_AUTH` | off (`false` in Bicep) | **Universal M2M shared-token auth (2026-07).** Lets **every** `require_caller`-gated route ALSO accept the shared `elb-openapi` `X-ELB-API-Token` (constant-time compared against the api-sidecar env / Redis cache token) in addition to the [MSAL](https://learn.microsoft.com/entra/identity-platform/msal-overview) bearer, so a peer-VNet automation caller manages one credential across the whole API surface — read AND write. OFF (default) = MSAL bearer only, unchanged. The shared token has NO Azure RBAC gate; enable this only when ingress access is otherwise controlled (private ingress, IP allowlist, VNet peering) — do not combine with wide-open public ingress. | [api/auth.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/auth.py) |
| `STRICT_RBAC_REMOVAL_HALT` | off (warn-only) | Makes the azd preprovision RBAC-removal preflight **halt** `azd provision` when a `Microsoft.Authorization/roleAssignments` resource would be deleted, unless `ACCEPT_RBAC_REMOVAL` is set for the run. See [charter §12a Rule 7](https://github.com/dotnetpower/elb-dashboard/blob/main/.github/copilot-instructions.md). | [scripts/dev/check_rbac_removal.py](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/check_rbac_removal.py) |

## Feature flags (behaviour switches, not hardening)

These select between two supported behaviours rather than tightening a safety
check. They do not follow the §12a Rule 4 hardening lifecycle.

| Gate | Default | Effect when `=true` | Read by |
| --- | --- | --- | --- |
| `BLAST_GATE_ENABLED` | off (legacy direct-submit path) | Routes BLAST submit through the AKS capacity gate instead of submitting directly. The `/api/blast/capacity` preview endpoint reports the would-have-been decision even when the gate is off. | [api/routes/blast/capacity.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/routes/blast/capacity.py), [api/tasks/blast/submit_task.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/tasks/blast/submit_task.py) |
| `SERVICEBUS_EXTERNAL_CONSUMER` | off (no demo observer) | Starts a worker-side **demo** consumer that drains the Service Bus completion topic into the in-memory event-bus (UI Live SB Events). The dashboard is the **producer** on `elastic-blast-completions`; the demo consumer exists only to exercise the contract. **Footgun (#78)**: by default the demo joins the shared `default` subscription alongside any real external integrator and they will compete-consume → 50/50 abandon storm visible as DLQ growth. Either point `SERVICEBUS_COMPLETION_SUBSCRIPTION` at a dedicated demo subscription or keep this gate OFF in environments with a real `default` subscriber. Startup emits a WARNING when this conflict would occur. | [api/services/service_bus_external_consumer.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/services/service_bus_external_consumer.py) |
| `BLAST_AUTO_RETRY_ENABLED` | off (no auto-resubmit) | Lets the `blast-auto-retry-failed-jobs` beat sweep auto-resubmit **transient submit-phase** failures (terminal sidecar / Azure auth / node-warmup) with bounded backoff, quarantining a job once its budget is exhausted. K8s runtime failures (`blast_search_failed`), cluster-state failures, and external-origin jobs are never auto-retried. Tunables: `BLAST_AUTO_RETRY_MAX` (default 2, 1–10), `BLAST_AUTO_RETRY_SWEEP_LIMIT` (resubmits per pass, default 5, 1–50), `BLAST_AUTO_RETRY_SCAN_LIMIT` (rows read per pass, default 200, 10–1000), `CELERY_BEAT_BLAST_AUTO_RETRY_SECONDS` (sweep interval, default 180). Planned flip date: TBD — keep OFF until the auto-retry UI affordance ships and one dogfood release cycle has soaked. | [api/tasks/blast/auto_retry_task.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/tasks/blast/auto_retry_task.py), [api/services/blast/auto_retry.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/services/blast/auto_retry.py) |
| `COST_PRICING_LIVE` | off (static price map) | Lets the Cost estimate card price VM SKUs from the public, no-auth [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices) (Linux on-demand, per region) instead of the hardcoded map, with a 24h in-process cache + static fallback on any fault. Off keeps the bundled approximate map (no external call). The card surfaces which source was used. | [api/services/cost/pricing.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/services/cost/pricing.py), [api/services/cost/estimate.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/services/cost/estimate.py) |
| `WEBHOOK_NOTIFICATIONS_ENABLED` | off (no outbound POST) | Lets the `dispatch-job-webhooks` beat sweep POST a provider-aware rich message (Slack Block Kit / Teams MessageCard / Discord embed; generic `{text, content}` for Logic Apps + custom integrators) to the configured webhook when a BLAST job reaches a terminal state. Requires a webhook configured + enabled in Settings → Webhooks. The URL is SSRF-validated against an allowlist (`hooks.slack.com` / `*.webhook.office.com` / Discord / Logic Apps, extendable via `WEBHOOK_ALLOWED_HOSTS`) on save AND at send time; https-only, IP literals rejected. Messages include a deep-link to the job (`{DASHBOARD_PUBLIC_URL}/blast/jobs/{job_id}` resolved via `api.services.control_plane_url`). Tunables: `WEBHOOK_SWEEP_LIMIT` (sends per pass, default 20, 1–100), `CELERY_BEAT_WEBHOOK_SECONDS` (sweep interval, default 60). | [api/tasks/webhooks.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/tasks/webhooks.py), [api/services/webhooks_pref.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/services/webhooks_pref.py) |

## Escape hatches (use only for the specific named situation)

These intentionally relax a safety check. They are not hardening toggles — they
exist so a known-safe operation is not blocked. Do not bake them into automation.

| Gate | Default | Effect when set | Read by |
| --- | --- | --- | --- |
| `ACCEPT_RBAC_REMOVAL` | unset | Overrides `STRICT_RBAC_REMOVAL_HALT` for a single run. The value must encode the phase-2 PR, e.g. `phase-2-of-pr-<N>`, and is cross-checked against the matching phase-1 PR at review. | [scripts/dev/check_rbac_removal.py](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/check_rbac_removal.py) |
| `ELB_ALLOW_SUB_MISMATCH` | unset | Lets `quick-deploy.sh` proceed when the active `az` login subscription differs from the azd env subscription. Needed when the azd env points at one tenant but you are logged into another. | [scripts/dev/quick-deploy.sh](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/quick-deploy.sh) |
| `ELB_ALLOW_AUTH_BYPASS_IN_CLOUD` | unset | Disarms the frontend deploy die-guard that aborts when `VITE_AUTH_DEV_BYPASS=true` would be baked into a cloud build. Only for a deliberate non-production sandbox. | [scripts/dev/quick-deploy.sh](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/quick-deploy.sh) |
| `ELB_SKIP_HOOKS` | unset | Skips the version-controlled pre-commit / pre-push CI-mirror git hooks for one command. Emergency use only — never push a red build knowingly. | [scripts/dev/install-git-hooks.sh](https://github.com/dotnetpower/elb-dashboard/blob/main/scripts/dev/install-git-hooks.sh) |

## Local-debug-only switches (never in a deployed Container App)

These change behaviour for a developer iterating from a laptop. Every one keeps
a `CONTAINER_APP_NAME` guard so a deployed Container App can never honour them.

| Gate | Default | Effect when `=true` | Read by |
| --- | --- | --- | --- |
| `AUTH_DEV_BYPASS` | false | Returns a synthetic `anonymous` caller (OID `00000…0`) instead of validating an [MSAL](https://learn.microsoft.com/entra/identity-platform/msal-overview) bearer token. The cloud `is_dev_bypass_caller()` guard rejects this identity even if it slips through to a deployed revision. Toggle the full local "real `az login`" session with `scripts/dev/local-run.sh auth-on` / `auth-off`. | [api/auth.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/auth.py) |
| `LOCAL_DEBUG_AUTO_OPEN_STORAGE` | false | Lets the local backend call `ensure_local_storage_access()` to open the workload Storage firewall to the caller's public IP when a route has full Storage ARM scope. Keeps the `CONTAINER_APP_NAME` guard so deployed apps can never flip Storage open. See [charter §9](https://github.com/dotnetpower/elb-dashboard/blob/main/.github/copilot-instructions.md). | [api/services/storage/public_access.py](https://github.com/dotnetpower/elb-dashboard/blob/main/api/services/storage/public_access.py) |
