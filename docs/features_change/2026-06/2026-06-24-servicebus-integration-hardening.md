---
title: Service Bus integration — signed download URLs, error detail on the completion topic, and a load harness
description: Let a completion-event consumer download result files by URL alone via a scoped signed token, surface a human-readable error_message on failure events (including drain-time rejections), and add a 500-1000 request load-test producer.
tags:
  - blast
  - auth
  - security
---

# Service Bus integration hardening (2026-06-24)

## Motivation

From the ElB Integration review notes:

1. **Download by URL alone** — a Service Bus completion consumer should be able
   to download a result file *without* performing a separate interactive login
   or minting a bearer token, while the dashboard keeps Storage private
   (charter §9 forbids handing a SAS / direct Storage URL to a consumer).
2. **Errors on the topic** — when a job fails, the completion topic event should
   carry *why* it failed, not just *that* it failed; and a request rejected
   before it ever bridges to a job (malformed body, permanent submit rejection)
   should still surface a terminal failure on the topic instead of going silent.
3. **Load pattern** — a repeatable 500-1000 request burst harness to measure the
   request-queue drain throughput / DLQ behaviour against existing infra.

## User-facing change

### 1. Signed, scoped download URLs (download by URL alone)

The `download_url` embedded on a `succeeded` completion event now carries a
short `?token=` when URL signing is available. The token is an HMAC signature
over `(version, job_id, file_id, expiry)` — **not** a Storage SAS and **not** a
direct Storage URL. It still points at the dashboard's own streaming gateway
(`GET /api/v1/elastic-blast/jobs/{job_id}/files/{file_id}`); the `api` sidecar
verifies the token and streams the bytes, so Storage stays private.

* A consumer downloads by fetching the URL — **no bearer token required**.
* The token authorises **exactly one** `(job_id, file_id)` until it expires
  (default 7 days, `DOWNLOAD_URL_TTL_SECONDS`).
* The signing key is **derived from the existing `EXEC_TOKEN`** Container Apps
  secret via domain separation — no new secret, no Bicep/infra change. The `api`
  (verify) and `worker` (mint) sidecars already carry `EXEC_TOKEN`.
* Reversible kill switch `DOWNLOAD_URL_SIGNED_TOKENS=false` stops minting new
  signed URLs; already-issued links keep working until they expire. When no
  signing key is present the bearer-only URL is emitted unchanged (safe default).
* The `GET /api/v1/elastic-blast/jobs/{job_id}/files/{file_id}` route now accepts
  **either** a valid `?token=` **or** a bearer (a valid token short-circuits; a
  browser request with a bearer and no token is unchanged).

### 2. Error detail on completion-topic failure events

* A `failed` `blast.transition` event now carries `error_message` (a sanitised,
  length-bounded human-readable detail from the sibling `error.message` /
  `error.detail`) in addition to `error_code`.
* A request rejected **at drain time** — a malformed body, or a permanent 4xx
  submit rejection — now publishes a terminal `failed` event to the completion
  topic (with `error_code` + `error_message`, `openapi_job_id=""`). Previously
  these never reached the topic because they never bridged to a sibling job, so
  a subscriber waited forever.
* Transient failures (5xx / 503 / 408 / 429 / transport) still ABANDON for
  redelivery and do **not** emit a terminal failure (the job will retry).

### 3. Load-test producer

`example/servicebus/load_test.py` enqueues N (default 500) requests onto the
existing request queue with unique run-correlated ids, batched, with a timing
report. It **never** creates Azure resources and **never** mutates shared config
(charter §13); it only sends to the namespace/queue already configured. Offline
`--self-test` and `--dry-run` included.

## Retry behaviour (verified, no code change)

The transient-vs-permanent classification the notes asked for was already
implemented and is now confirmed by the regression suite: permanent 4xx →
dead-letter (no retry), transient 5xx/503 and retryable 408/429 → abandon →
Service Bus redelivery (maxDelivery 10), malformed → dead-letter. The new
drain-time failure publish does not change any settlement decision.

## OutFmt 7 result format / merge (not in this change)

The `content=full|merged|xml` selector is a parameter of the **sibling**
elb-openapi `GET /v1/jobs/{job_id}/results` endpoint (documented in the API
Reference UI), and the "outfmt 7 merge" question is a sibling merger / live
behaviour item. It needs a live outfmt-7 Service Bus run + sibling inspection
and is intentionally **not** changed here.

## API / IaC diff summary

* `api/services/download_token.py` — new HMAC mint/verify service (no new secret;
  derives from `EXEC_TOKEN`).
* `api/auth.py` — new `require_caller_or_download_token` gate + synthetic
  download-token identity (mirrors `require_caller_or_openapi_token`).
* `api/routes/elastic_blast.py` — download route accepts `?token=` and uses the
  new gate.
* `api/tasks/servicebus/tasks.py` — mint the token onto the completion-event
  `download_url`; add `error_message` to failure events; publish a terminal
  failure event on drain-time malformed / permanent-rejection.
* `example/servicebus/load_test.py` — new load harness; `consume.py` messaging
  updated to reflect token-based download.
* **No Bicep / Container App template change** — the feature rides the existing
  `EXEC_TOKEN` secret.

## Validation evidence

* `uv run pytest -q api/tests/test_download_token.py` — 10 passed (mint/verify,
  expiry, scope binding, tampering, kill switch, missing key).
* `uv run pytest -q api/tests/test_external_blast_api.py -k download_file` —
  3 passed (valid token without bearer streams; missing auth → 401; cross-file
  token → 401).
* `uv run pytest -q api/tests/test_servicebus_tasks.py` — error_message on
  failure events, drain-time failure publish (permanent + malformed), and signed
  `download_url` build all covered; full file green.
* `uv run pytest -q api/tests/test_route_contracts.py api/tests/test_persona_matrix.py`
  — 58 passed (auth gate swap does not break route contracts or persona matrix).
* `uv run ruff check` — clean on all touched files.
* `python3 example/servicebus/load_test.py --self-test` — OK.

## Deployment / live verification

Deployed to the running customer environment as a code-only image deploy
(`scripts/dev/quick-deploy.sh api` → api/worker/beat on revision
`ca-elb-dashboard--0000148`). **No infra change** — the signed-URL path rides the
existing `EXEC_TOKEN` secret already present on the api + worker sidecars.

Live end-to-end proof of "download by URL alone, no bearer" against the live api,
using a real completed job's result file (`92869d3eb92c` / `result-001`, a
`core_nt` shard `.out.gz`). A token was minted with the same `EXEC_TOKEN`-derived
key the api verifies with:

* **Signed `?token=`, NO `Authorization` header → HTTP 200, 13078 bytes (exact),
  valid gzip, real BLAST output** (`# BLASTN 2.17.0+ … # Database:
  core_nt_shard_00`). Download by URL alone works.
* **No token, no bearer → HTTP 401** — the route is not anonymous; the security
  boundary holds (charter §9: still the dashboard gateway, never a SAS).
* **Tampered token, no bearer → HTTP 401** — signature verification works.

The fact that a locally-minted token verified on the live api confirms the
mint (worker) and verify (api) sides share the key and algorithm; the worker's
mint-onto-completion-event path is additionally covered by
`test_result_files_for_event_signs_download_urls_when_enabled`.

### Error-on-topic (③) and queue load (④) — live-verified on the customer env

With operator approval to proceed in the customer environment, ③ and ④ were
exercised live (revision `0000150`), entirely through the dashboard's own managed
identity (the caller has no direct RBAC on the customer's production Service Bus
namespace, so every interaction went through the dashboard API):

* **③ error-on-topic** — a `db=/deadletter-probe` request was enqueued via
  `POST /api/settings/service-bus/send`. The worker log captured the drain-reject
  path executing live: `service bus → OpenAPI submit rejected (dead-letter)
  corr=dlprobe2-… status=400`, and the DLQ incremented accordingly. That code
  path unconditionally calls `_publish_drain_failure_event`, which publishes a
  terminal `failed` topic event carrying `error_code=servicebus_submit_rejected_400`
  + the sanitised `error_message`. (The exact event payload is asserted by
  `test_drain_publishes_failure_event_on_permanent_rejection`; the demo
  observer-consumer was not used to read it back because the customer topic has
  no `playground-observer` subscription and the caller cannot create one.)
* **④ queue load** — a 15-message burst was enqueued back-to-back. The queue
  absorbed all 15 (`active` rose), the drain processed every message, and the DLQ
  delta was **exactly +15 with `active` returning to 0 — no loss, no stuck
  messages**, confirming the queue/drain/DLQ path handles a concurrent burst. The
  mechanism is scale-invariant (Service Bus Standard buffers thousands), so the
  500-1000 figure is a capacity question, not a code-path question; a 500-1000
  real-BLAST burst was intentionally NOT run because it would consume hours of the
  customer's production cluster compute for no additional code coverage.

**Cleanup performed in the same session**: the 17 test DLQ entries were purged by
sequence number (the 3 pre-existing customer entries were left untouched), and
the temporary `SERVICEBUS_EXTERNAL_CONSUMER` worker env flag was reverted
(revision `0000150`, demo consumer off). No Azure resource was created and no
shared config row was repointed (charter §13).
