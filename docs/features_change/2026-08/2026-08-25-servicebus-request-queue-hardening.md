---
title: Service Bus request queue reliability hardening
description: Preserve execution targets, bound fallback submits, prevent unsafe reconfiguration, and keep deferred or corrupt response rows from starving completion delivery.
tags: [blast, operate]
---

# Service Bus request queue reliability hardening

## Motivation

A read-only review of the [Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview) request path found reliability gaps beyond the previously repaired bridge-etag and AKS-start-admission incidents.

Recent [Application Insights](https://learn.microsoft.com/azure/azure-monitor/app/app-insights-overview) telemetry provided one concrete deadline signal. During the 48-hour review window, 270 requests reached `accepted`, 14 were safely deferred by OpenAPI readiness admission, one scheduled retry followed an OpenAPI 503, and one fallback drain converted `SoftTimeLimitExceeded` into an ordinary scheduled retry. That request recovered on the next delivery, but its acknowledgement was delayed. Response-outbox health remained at zero pending/deferred/poison rows throughout the sampled window, so the outbox findings below were latent rather than an active incident.

The code review also reproduced these deterministic failures:

- An explicitly scoped API request entered the deployment-wide queue and lost its requested subscription/resource group/cluster routing.
- Queue translation discarded target scope and a custom `/v1/jobs` `resource_profile`.
- Service Bus settings allowed queue or execution routing to change while requests and active bridges still depended on the old configuration.
- A concurrent non-routing settings PUT could overwrite the entire config row with a stale snapshot and silently revert a fenced routing update.
- A drain that read configuration before a routing update could acquire the queue lease after that update and consume a new-target message with its stale snapshot.
- A Redis restart could erase a live drain lease; without a final durable config read, an already-received message could submit to the old target after Settings changed.
- Deployment-level queue/topic overrides could leak into the persisted Table row when the effective GET payload was saved unchanged.
- The fallback Celery task could start another receive wave before its 45-second soft deadline and used the general 90-second OpenAPI timeout/retry policy.
- The Azure Table response-outbox query selected an arbitrary bounded RowKey window before considering retry deadlines; future-deferred or corrupt rows could repeatedly hide ready responses.
- A sender response lost after broker acceptance returned a generic failure without the server-generated correlation id, so a user retry could create a second logical request.
- Queue payload validation accepted inline FASTA up to 10 MB even though the Service Bus request envelope is deliberately much smaller.
- Post-submit bridge confirmation failures left an unconfirmed claim instead of immediately retrying the already-idempotent sibling request.

## User-facing change

- API submits with an explicit subscription, resource group, or cluster stay on the direct OpenAPI path. Unscoped submits can still use the optional Service Bus ingress.
- Dashboard-produced queue messages retain the existing JSON body and envelope. No subscription, resource group, cluster, or storage field is injected. External messages that already provide explicit target fields are validated against the active deployment target; messages without them remain backward-compatible.
- `/v1/jobs` queue messages retain caller-selected resource profiles instead of silently reverting non-`core_nt` requests to `standard`.
- Service Bus routing changes take a drain stop-intent fence and return `409 servicebus_reconfigure_busy` while a drain is active. They return `409 servicebus_reconfigure_blocked` while the current request queue, dead-letter queue, active bridge set, or response outbox still has dependent work. If that state cannot be verified, the update fails closed with `503 servicebus_reconfigure_state_unavailable` and leaves the saved config unchanged.
- Every full-row settings PUT takes a deployment-wide mutation mutex before reading the current config. This serializes concurrent saves, while only actual routing changes take the queue stop-intent and empty-state checks.
- Settings responses carry an opaque `revision`. The first update upgrades a legacy revisionless row; later stale or revisionless full-row saves return `409 servicebus_config_changed` instead of overwriting a concurrent winner.
- Deployment queue/topic/kind overrides are applied to runtime copies only. Saving another field preserves the raw stored entity names, so removing an env override restores the prior persisted target.
- Request endpoint migration checks both the current and proposed queue/DLQ; pre-populated work on either side blocks the update.
- After acquiring the queue lease, a drain re-reads the active routing signature and exits before receive if its original config snapshot is stale.
- Every claimed message repeats the durable routing check immediately before admission/submit, closing the Redis-restart lease-loss window.
- Transition/outbox publishing, DLQ reconciliation/cleanup, and manual queue mutations hold the same queue-scoped config-I/O token set as internal request sends and revalidate the complete config after acquisition. Settings cannot cross an active old-target operation.
- The Celery fallback drains one concurrency-sized receive batch under a 40-second work budget, derives a maximum 35-second OpenAPI timeout from the time remaining after admission, reserves five seconds for staging/settlement, uses zero internal transport retries, and defers 401 token repair to the durable queue. The resident consumer retains the historical 90-second OpenAPI timeout, transport retries, and inline token self-heal.
- Celery soft deadlines now propagate instead of being recorded as ordinary submit failures. A won bridge claim is released first so redelivery can resume cleanly.
- Response-outbox flushing queries due rows before applying its bounded scan. Malformed payload rows are never published: payload-free metadata is durably appended to the DLQ audit backup before the active outbox row is removed; an audit failure keeps and defers the row for 24 hours.
- Existing request-queue wire behavior is preserved: producers keep `json.dumps(body, default=str)`, do not assign a new `MessageId` unless the caller supplied one, and retain the historical retry `MessageId` formula. An ambiguous send returns `send_outcome_unknown` with the reusable correlation id, while requests above the 192 KiB serialized wire budget fail before broker I/O (`413 request_too_large` in the Playground; direct fallback in unified API ingress).
- A sibling 2xx response without `job_id` and a post-submit bridge persistence failure both release the claim and follow the bounded idempotent retry path instead of completing an untrackable request.
- The reconfiguration stop-intent fences drains and all tracked config-dependent data-plane mutations. Direct external namespace producers still require an operator-coordinated pause for a namespace/request-queue migration.
- Tracked operations hold independently expiring Redis tokens through broker I/O; Settings atomically removes expired tokens and acquires its stop-intent only when both the drain lease and token set are empty. Token-specific release prevents a stale operation from releasing a newer lease. Coordination fails closed on Redis errors, while the unified API retains its direct fallback.
- Producer tokens use a 900-second interrupted-process backstop and shrink to a 60-second visibility grace after a confirmed or ambiguous broker attempt.
- The routing stop-intent and full-row config mutation mutex use the same 900-second crash backstop, so a slow management-plane safety check cannot outlive its fence.
- Every Redis coordination primitive now enforces that 900-second floor at its own boundary, even if a future caller passes a shorter custom value. Concurrent send acquisition extends but never shortens the shared token-set TTL, so a shorter later caller cannot expire an older live token.
- Best-effort lease release failures now log at warning level; the token-owned release and TTL crash backstop remain unchanged.
- Internal senders with persisted config revisions re-read the routing signature after acquiring the token, closing the stale-config window before broker I/O without changing existing Entra or SAS authentication behavior.
- Confirmed and ambiguous broker sends retain a 60-second visibility token so delayed runtime counters cannot expose an apparently empty queue to Settings. Drain lease acquisition now fails closed on Redis errors because the lease is also the routing-mutation fence.
- The drain lease is mandatory; a legacy `SERVICEBUS_DRAIN_SINGLEFLIGHT=false` override no longer disables coordination because an untracked drain would invalidate the Settings fence.
- `SERVICEBUS_DRAIN_LOCK_TTL_SECONDS` now has a 900-second safety floor, preventing an override from expiring the routing fence during a live resident drain.
- A soft-timed-out drain retains its lease until that TTL backstop instead of exposing still-unwinding submit threads to a Settings routing change.
- The Celery task's remaining work budget is passed into the generic drain loop, not only the submit handler, so future batch changes cannot outlive the five-second settlement reserve.
- The fallback does not start a receive pass with less than the generic drain loop's one-second minimum window, and it releases a won bridge claim without submitting when less than the OpenAPI transport's 0.5-second minimum remains above the five-second settlement reserve.
- Expired unconfirmed bridges must win the atomic stale claim before publishing `bridge_unconfirmed_timeout`; a concurrent confirmation wins instead of receiving a contradictory failure.
- Malformed outbox retry counters are quarantined per row and cannot abort delivery of valid rows.
- A per-row defer persistence failure no longer stops unrelated correlations in the same bounded flush pass, and `deferred_timestamp_corrupt` rows have a distinct payload-free health warning.

## API and runtime summary

- Added pure target-integrity helpers under `api.services.service_bus_target`.
- Added optional `timeout_seconds` and `max_transport_retries` keyword arguments to the internal OpenAPI submit client; callers that omit them retain the previous behavior.
- Raised the stale bridge-claim floor from 120 seconds to 900 seconds so it exceeds the resident consumer's complete three-attempt OpenAPI timeout, stale-token retry, token-resync, and backoff envelope.
- Added `SERVICEBUS_TASK_SUBMIT_TIMEOUT_SECONDS`; invalid or non-finite values fail safe to 35 seconds and values are clamped to 5-35 seconds.
- Added bounded due-response and corrupt-row handling to the Azure Table outbox repository.
- Added payload-free `outbox_corrupt_response_pending` / `outbox_timestamp_corrupt_pending` health telemetry and structural tests that reject any Service Bus broad catch which omits `SoftTimeLimitExceeded` propagation.
- No Service Bus entity, Azure role, network policy, Storage account, Container App layout, or other infrastructure resource changed. No deployment or live BLAST submit was performed for this change.

## Validation

- Read-only 48-hour Application Insights review of Service Bus request and health events.
- Focused target/deadline/outbox regression tests, including pre-fix failure reproduction.
- Adversarial hardening critique covered state contracts, Redis atomicity, concurrent Settings writes, message boundaries, deadlines, claim idempotency, outbox/DLQ recovery, API security, observability, restart behavior, frontend compatibility, and final post-fix exit gates. The final compatibility pass removed target-field injection, automatic request `MessageId` generation, canonical request serialization, retry digest IDs, and SAS namespace enforcement so the existing request wire and security behavior remain unchanged.
- Service Bus focused sweep: `650 passed`; Persona Matrix: `53 passed`.
- Full backend suite: `5266 passed, 4 skipped`. Three skips require an explicit candidate result directory; one upstream program-enum sync guard was skipped because the read-only sibling source clone is not present on this machine.
- `uv run ruff check api` — passed.
- Focused strict typing (`mypy --strict --follow-imports=skip`) passed on eight clean changed modules. Six compatibility facades passed with only the known pre-existing `no-any-return`, `union-attr`, `unused-ignore`, and FastAPI `untyped-decorator` debt codes disabled; the initial broad run reported only those existing diagnostics and no new hardening-line error.
- Frontend: `108` Vitest files / `978` tests passed; ESLint, Prettier, and the production TypeScript/Vite build passed.
- Local-safe Playwright fullstack: `43 passed, 6 skipped` (`ui-mock`, `api-smoke`, and `mutation-mock`; all six skips require explicit live mutation/BLAST opt-in). Non-live API BLAST smoke: `1 passed, 1 live-submit test skipped`.
- Bundled local API smoke against `http://127.0.0.1:8090`: `27/27 passed`.
- `uv run python scripts/docs/check_frontmatter.py` — passed (`61` navigated pages).
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` — passed.
