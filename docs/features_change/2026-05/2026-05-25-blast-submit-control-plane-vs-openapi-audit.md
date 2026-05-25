# BLAST submit hardening — control-plane direct vs OpenAPI path audit

**Date**: 2026-05-25
**Scope**: Behaviour-preserving improvements to the two BLAST submit code paths.

## Motivation

The dashboard exposes two distinct BLAST submit code paths that the SPA and
external API consumers can route through:

1. **Control-plane direct** — `POST /api/blast/submit` (and `POST /api/blast/jobs`
   when the body omits `query_fasta`). Validates the request, persists a
   `JobState`, enqueues a Celery `blast.submit_job` task, and runs the BLAST
   pipeline from inside the worker / terminal sidecars against the local AKS
   cluster.
2. **OpenAPI execution plane** — `POST /api/blast/jobs` when the body contains
   `query_fasta`. Forwards the request to the sibling `elb-openapi`
   Deployment in AKS via `api/services/external_blast.py`, then surfaces the
   returned `job_id` under the same `/api/blast/jobs/{id}` namespace.

A full audit was run against both paths (two session-memory maps,
`blast-submit-hardening-map.md` and `openapi-submit-map.md`) covering 100+
candidate improvements across rate limiting, observability, error truncation,
type signatures, retry policy, variable shadowing, payload validation, magic
numbers, sanitisation, RBAC scoping, and idempotency semantics.

This change ships the subset that is **behaviour-preserving** — no observable
contract change for SPA clients or sibling OpenAPI consumers, except for one
explicit bug fix (the meta.request_id silent-None on `POST /api/blast/jobs`).
The larger items that *do* change observable behaviour are listed in
"Parked for separate decision" below and require maintainer approval before
shipping.

## User-facing change

* `meta.request_id` on the response of `POST /api/blast/jobs` (OpenAPI delegation
  path) is now populated with the actual per-request id from
  `RequestIdMiddleware`. Previously it was silently `None` because the local
  variable shadowed the FastAPI `request` parameter (see API/IaC diff #1
  below). The 202 status, `Location`, `Retry-After`, `dashboard_job_id`,
  `openapi_job_id`, `operation`, `target`, and `admission` fields are
  unchanged.
* Sibling-service transport-error messages surfaced as
  `503 {"code": "openapi_unreachable", "message": "..."}` are now passed
  through `api.services.sanitise.sanitise()` before truncation. SAS tokens,
  bearer tokens, Azure keys, base64 blobs ≥40 chars, and connection-string
  fragments that may have been embedded inside an `httpx` exception string
  (for example a retried URL that included credentials) are masked.
  Subscription / tenant / object GUIDs are also masked by default
  (`first-8…`). The truncation cap (300 chars) is preserved.
* `POST /api/blast/pre-flight` database-availability fail `detail` and
  `POST /api/blast/jobs/{job_id}/file` `invalid_config_payload` message are
  now `sanitise()`-wrapped for the same reason. Truncation caps (300 / 500
  chars) are preserved.

## API / IaC diff

### 1. Variable shadowing bug fix — `api/routes/blast/submit.py::blast_job_submit`

The route signature carries `request: Request` (FastAPI parameter). The body
then did:

```python
request = ExternalBlastSubmitRequest(**body)
```

which shadowed the FastAPI parameter. By the time the `return` statement
reached `request_id_from_scope(request)`, `request` was a pydantic model with
no `.state` attribute, so the helper silently returned `None` and every
response carried `meta.request_id == None`.

Fix: rename the local model variable to `submit_request`. The FastAPI
`request` parameter is now visible at the `request_id_from_scope(request)`
call site and the request id flows into `meta.request_id`.

### 2. Magic-number consolidation

* `api/routes/blast/submit.py`
  * `_EXCEPTION_DETAIL_MAX_CHARS = 500` — replaces three repeated `[:500]`
    truncation literals in the three `except` blocks that surface
    `validation_error` / `sharding_precision_invalid` details.
  * `_SUBMIT_RETRY_AFTER_SECONDS = 5` — replaces three `Retry-After = "5"`
    header writes and two `poll_after_seconds: 5` admission payloads.
* `api/services/external_blast.py`
  * `_TRANSPORT_DETAIL_MAX_CHARS = 300` — replaces five repeated `[:300]`
    truncation literals on `httpx`-derived 503 messages.
  * `_SANITISE_DETAIL_STRING_MAX_CHARS = 1000`, `_SANITISE_DETAIL_KEY_MAX_CHARS
    = 100`, `_SANITISE_DETAIL_LIST_LIMIT = 20` — replace inline literals in
    `_sanitise_detail()`.
  * `_SAFE_FILENAME_MAX_LENGTH = 128` — replaces inline literal in
    `_safe_filename()`.

All replacements preserve the original numeric values verbatim. Behaviour is
identical.

### 3. Sanitise wrap on exception-message surfaces

| File | Site | Before | After |
|------|------|--------|-------|
| `api/services/external_blast.py::submit_job` | line ~214 | `str(exc)[:300]` | `sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS])` |
| `api/services/external_blast.py::get_job` | line ~279 | `str(exc)[:300]` | `sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS])` |
| `api/services/external_blast.py::list_jobs` | line ~305 | `str(exc)[:300]` | `sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS])` |
| `api/services/external_blast.py::download_file` | line ~330 | `str(exc)[:300]` | `sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS])` |
| `api/services/external_blast.py::stream_file` | line ~367 | `str(exc)[:300]` | `sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS])` |
| `api/routes/blast/preflight.py::blast_pre_flight` | line ~165 | `str(exc)[:300]` | `sanitise(str(exc))[:300]` |
| `api/routes/blast/results.py::blast_job_file` | line ~163 | `str(exc)[:500]` | `sanitise(str(exc))[:500]` |

`sanitise()` is the same helper already used by `api/services/external_blast._sanitise_detail()`
and by every route that mediates a sibling response — extending it to the
exception-message surfaces closes the leak window where an httpx URL or a
Storage SDK ARM error embeds credentials. Truncation caps are preserved
exactly.

## Behaviour-preservation rationale

* No status codes, header names, response shapes, JSON field names, JSON field
  ordering, log line shapes, or admission-decision strings change.
* Sanitiser-masked tokens / GUIDs are an explicit security improvement: the
  pre-change strings were defects that surfaced credential material to SPA
  consumers. Subscription-ID masking is on by default in `sanitise()` and is
  consistent with how every other route in the repository now surfaces
  ARM errors.
* The variable-shadowing fix is technically observable
  (`meta.request_id` moves from always-`None` to actual id) but it qualifies
  as a bug fix: the field was specified to carry the request id and silently
  failed. No existing test asserts the field is `None`.

## Parked for separate decision (not shipped in this change)

These items showed up in the audit but each one **does** change observable
behaviour and requires maintainer approval:

1. **Rate-limit symmetry on `/api/blast/submit`.** The OpenAPI proxy and
   `/api/v1/elastic-blast/*` routes are guarded by
   `api/app/openapi_rate_limit.py` (2000 req / 60 s per token). The direct
   control-plane submit is unthrottled. Adding a limit changes 429 surfacing
   for current SPA users.
2. **Cross-user external job visibility.** `api/routes/blast/jobs.py`
   `_sync_external_jobs_to_table(caller_oid="")` intentionally attributes all
   sibling-originated jobs to the empty owner. Tightening this would hide
   jobs that currently appear in every user's list. The comment in the
   source flags this as a deliberate UX choice.
3. **External job delete route.** No `DELETE /api/blast/jobs/{id}` path for
   sibling-originated jobs; the sibling has its own cleanup. Adding the route
   is new functionality.
4. **Polling cap behaviour.** SPA polls under TanStack Query; the dashboard
   does not enforce a global polling cap. Introducing one changes when
   long-running jobs stop being polled.
5. **Idempotency-replay detection symmetry.** The control-plane path returns
   the existing JobState when `idempotency_key` collides. The OpenAPI path
   has no equivalent — it always forwards. Adding parity touches sibling
   contract assumptions.
6. **Status vocabulary normalisation** (`dispatching` / `submitting` →
   `running` at the SPA boundary). The status set is part of the contract;
   collapsing values would simplify charts but breaks consumers that filter
   on the granular values.
7. **Orphan-on-timeout recovery for sibling submit.** When the sibling
   `submit_job` exceeds `_DEFAULT_TIMEOUT_SECONDS = 90.0` but the sibling
   accepted the job server-side, the dashboard records a 503 without a
   `dashboard_job_id` row. A reconciler that finds the sibling job by
   `external_correlation_id` is a new background job.

## Validation evidence

* `uv run ruff check api/routes/blast api/services/external_blast.py
  --output-format=concise` — `All checks passed!`
* `uv run pytest -q api/tests/test_external_blast_api.py
  api/tests/test_blast_submit_route_options.py
  api/tests/test_route_contracts.py` — **64 passed in 6.54s**
* `uv run pytest -q api/tests/` — **1436 passed, 2 failed**.
  * Both failures (`test_preflight_returns_admission_decision`,
    `test_run_truncates_stdout_above_cap`) were confirmed pre-existing by
    running the failing tests on a clean stash; they are unrelated to this
    change.

## Files changed

```
api/routes/blast/submit.py
api/routes/blast/preflight.py
api/routes/blast/results.py
api/services/external_blast.py
```

Net diff is ~60 LOC across the four files, almost entirely
constant-extraction + four lines of behavioural change (variable rename for
the bug fix + sanitise() wrap).
