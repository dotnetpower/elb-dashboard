# Security audit 2026-05-22 — items #3, #6, #7

## Motivation
Read-only security sweep of the api sidecar surfaced 20 findings ranked by
severity. The three lowest-risk-to-patch and highest-immediate-impact items
were addressed in this change:

- **#3 (CRITICAL)** — Three Celery diagnostic endpoints under `/api/health/celery*`
  were unauthenticated. They exposed broker URL, worker stats, and arbitrary
  task results, and accepted anonymous enqueue. Any unauthenticated caller
  could (a) inventory tenant infrastructure detail and (b) burn worker
  capacity by spamming `enqueue-noop`.
- **#6 (HIGH)** — `/api/operations/{id}` and the legacy alias `/api/tasks/{id}`
  validated the bearer token but did not check ownership of the underlying
  job. A logged-in tenant member could poll any other user's Celery task and
  read its result payload (BLAST job metadata, ARM responses, error
  tracebacks).
- **#7 (HIGH)** — `/api/audit/log` returned the raw `payload_json` blob stored
  in the jobhistory table. The blob may legitimately contain SAS query
  strings, bearer tokens, and subscription ids that audit consumers must not
  see in raw form.

## User-facing change
- Calling `GET /api/health/celery`, `POST /api/health/celery/enqueue-noop`, or
  `GET /api/health/celery/result/{task_id}` without a valid MSAL bearer now
  returns `401 invalid token` (or `401 missing bearer token`).
- `GET /api/operations/{id}` and `GET /api/tasks/{id}` return
  `403 {"detail": "not owner"}` when the JobState row for the task records a
  different `owner_oid`. Tasks without a JobState row (system / diagnostic
  tasks such as `diag_noop`) remain accessible — they carry no per-user
  payload.
- `GET /api/audit/log` redacts SAS query strings, bearer tokens, account /
  access keys, connection strings, and Azure GUIDs from the `payload` field
  before returning. The repo layer still stores raw blobs for forensic use;
  redaction is a presentation-boundary concern.

## API / IaC diff summary
| Layer | File | Change |
|---|---|---|
| Routes | [api/routes/health.py](../../../api/routes/health.py) | Added `Depends(require_caller)` to the three `/health/celery*` endpoints. |
| Routes | [api/routes/operations.py](../../../api/routes/operations.py) | New `_enforce_task_ownership` helper; lookup JobState by task_id, return 403 if `owner_oid` differs from caller. |
| Routes | [api/routes/tasks.py](../../../api/routes/tasks.py) | Same ownership gate as operations.py (legacy alias parity). |
| Routes | [api/routes/audit.py](../../../api/routes/audit.py) | Wrap `payload_json` with `api.services.sanitise.sanitise` before returning. |
| Services | [api/services/state_repo.py](../../../api/services/state_repo.py) | New `JobStateRepository.find_by_task_id(task_id)` returns the summary row matching a Celery task id (uses `_JOBSTATE_SUMMARY_SELECT`, no payload). |
| Tests | [api/tests/test_smoke.py](../../../api/tests/test_smoke.py) | Added the three `/health/celery*` paths to the anonymous-rejection parametrised test plus four new regression tests for ownership (403 / 200 paths) and audit sanitisation. |

No IaC changes. No new dependencies. No deploy required — backend routes only.

## Validation evidence
- `uv run ruff check api` — passed.
- `uv run pytest -q api/tests` — **883 passed in 24.24s** (was 871 → +12 new
  parametrised + regression cases).
- Targeted run: `uv run pytest -q api/tests/test_smoke.py` — 73 passed.

## Hardening pass (same day)
A self-critique surfaced three additional weaknesses; fixed in the same
change:

- **CRITICAL — ownership check failed *open* on lookup error.** The first
  draft swallowed every state-repo exception and let the request through
  with a log line. That is exactly the "try/except blind" pattern the user
  memory warns against. Fixed:
  - `_enforce_task_ownership` now fails **closed** with HTTP 503
    `{"code": "ownership_check_unavailable", "retryable": true}` when the
    state lookup raises.
  - `AUTH_DEV_BYPASS=true` is the single exception (dev loop without a real
    state backend would otherwise hard-fail on every poll); the synthetic
    identity is already trust-flagged.
  - `JobStateRepository` import promoted to module scope so the test
    `monkeypatch` target is unambiguous and the helper has no hidden hot
    import cost.
- **HIGH — audit fallback leaked raw exception strings.** State-repo /
  Storage SDK exceptions routinely embed account URLs, request-id GUIDs,
  and occasionally SAS query strings. The fallback now passes through
  `api.services.sanitise.sanitise` before truncation.
- **Coverage gap.** The original `pragma: no cover - defensive` masked the
  degraded branch. New regression tests:
  - `test_operations_fails_closed_when_state_repo_raises_in_production` —
    asserts 503 + `ownership_check_unavailable` when dev bypass is off.
  - `test_operations_fail_open_in_dev_bypass` — asserts 200 + Celery
    projection when dev bypass is on.
  - `test_audit_log_error_branch_is_sanitised` — asserts SAS / subscription
    GUID redaction in the fallback `error` string.

## Non-goals (deferred to follow-up notes)
- #1 / #4 (role-based authz on top of bearer validation) and #2 (per-ticket
  tmux session) need a design pass before any code change. Drafts live in
  [docs/copilot/security-audit-followup.md](../../copilot/security-audit-followup.md).
- #5, #8, #9, #11, #12 remain open as tracked items in the audit memory note.
