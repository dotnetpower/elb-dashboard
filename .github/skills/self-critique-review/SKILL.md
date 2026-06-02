---
name: self-critique-review
description: "Use BEFORE writing risky code (design pass) AND before task_complete (final pass) on any non-trivial backend/infra change, OR whenever the user asks for a critique / review / self-review (often phrased in Korean as 비평 / 검수). Catches the Critical/High design defects a post-hoc critique usually surfaces — contract/state-machine consistency, unbounded retry/wait loops, idempotency, concurrency races, partial-failure, observability — that the mechanical self-review (consumer grep + tests + diff) cannot see. Trigger phrases: critique this change, self-review, design review, re-check for problems, is this correct now."
---

# Self-Critique Review

## Why this exists

The charter's post-implementation self-review (§13) is **mechanical**: grep
consumers, run the wide test suite, ruff/build, diff audit, fixture parity. That
confirms *"does it still work?"* — it cannot see **design-level** defects, which
is exactly where the recurring Critical/High critique findings come from. A bug
like returning `status="queued"` on a re-enqueue passes every focused test yet
silently breaks the reconciler contract; an unbounded warmup re-enqueue loop has
green tests yet never surfaces a stuck job as Failed.

This skill is the **design lens** that runs *in addition to* §13:

- **Design pass** — apply the rubric while planning (charter §0 step 3) so the
  defect is engineered out, not patched in afterwards.
- **Final pass** — apply it again before `task_complete`, then report the
  outcome in the user-facing message alongside the §13 mechanical results.

Speak to the user in Korean; keep all files/commits/docs/code in English.

## How to run it

For each changed function / route / task / state field / IaC resource, walk the
rubric below. For every item, either (a) assert it is satisfied with a concrete
reason, or (b) flag it as a finding (Critical / High / Medium) with the file +
line and a fix. Do **not** hand-wave "looks fine" — name the consumer, the loop
bound, the failure branch. If a Critical/High has no fix yet, do not call
`task_complete`; fix it or escalate to the user with the specific blocker.

Skip an item only when it provably does not apply (e.g. no loop → skip the
liveness item) and say so.

## The rubric (recurring Critical/High classes)

### 1. Contract & state-machine consistency  *(the #1 source of silent breakage)*
- Does any **other** component consume the value I changed (a return dict
  `status`/`phase`, a Table field, a JSON payload key, a TS type)? List them:
  reconciler (`_celery_success_row_status`), pollers, beat reconcilers, the
  frontend `classifyJobState` / `PHASE_TO_STEP` / timeline predicates, other
  routes reading the same row.
- For a Celery task that **re-enqueues / returns early**: what does
  `reconcile_stale_jobs` do when it sees the original task's SUCCESS while the
  job is still waiting? Only `status="running"` stays active; anything else is
  reconciled to `completed` and fires the artifact finalizer on a not-yet-run
  job. (This is the literal `status="queued"` bug.)
- Did I add a new `status`/`phase`/`error_code` string? Is it mapped in EVERY
  switch that already handles its siblings (backend reconciler + frontend
  badge/step mapping + failure-text predicates)?

### 2. Liveness / bounded loops / failure modes
- Every retry / wait / re-enqueue / poll loop: is there an **upper bound**
  (deadline, max attempts, TTL)? What happens when the underlying condition is
  *permanently* stuck — does it eventually reach a terminal Failed state, or
  loop forever invisibly?
- Re-enqueue (`apply_async`) does NOT consume `max_retries` and has no backoff
  ceiling → it MUST carry its own deadline (see the warmup
  `warmup_wait_deadline_ts` pattern). `task.retry` is bounded by `max_retries`
  but caps the total wait — pick deliberately and justify.
- Timeouts on every external call (HTTP, subprocess, K8s, ARM)? Slowloris /
  hung-stream protection on any stdlib server?

### 3. Idempotency & concurrency
- If this task/route runs twice (Celery redelivery, user double-click, beat
  overlap) are the side effects safe? Any read-modify-write that needs a lock /
  atomic op / `flock`?
- Two workers racing the same resource (submit lock, capacity slot, state row):
  who wins, and does the loser degrade gracefully (retryable wait, not error)?

### 4. Partial failure / fan-out
- In a `ThreadPoolExecutor` / `asyncio.gather` fan-out, if ONE future raises,
  what happens to the others and to the overall result? Is the exception
  surfaced or swallowed?
- Best-effort side effects (oracle uploads, log persistence) clearly separated
  from must-succeed ones, and their failure degrades (not aborts)?

### 5. Security & boundary (charter §12 / §12a)
- New route validates the MSAL bearer (`require_caller`)? WebSocket handshake
  too? SSE stays ticket-based (never add `Depends(require_caller)` to an event
  stream).
- No SAS token to the browser; no Storage `publicNetworkAccess` flip in a
  production path; `ttyd`/exec_server bind `127.0.0.1` only.
- RBAC change is single-PR safe (no role narrowed) OR labelled phase-1/phase-2.
- Output shown in the UI is sanitised (no tokens / SAS / subscription IDs).

### 6. Observability & terminal state
- On failure, is there a log line AND a terminal state row the dashboard can
  render? Terminal BLAST writes go through `_update_state` (sweeps orphan
  `running` steps), NOT `repo.update(...)` directly.
- New `error_code` is greppable and documented in the change note?

### 7. Backward compatibility
- New field optional/nullable with a safe default? Removed field has a
  deprecation path? Renamed symbol keeps a re-export shim if any caller (incl.
  tests / `web/src/mocks/**` fixtures) might still use it?
- New keyword-only param defaulted so existing call sites keep compiling?

## Output format

Report a short verdict the user can scan (deliver it to the user in Korean,
keep the field labels themselves as written):

```
Design critique (self-critique):
- Contract: <ok, or finding + location + fix>
- Liveness/loops: <…>
- Idempotency/concurrency: <…>
- Partial failure: <…>
- Security: <…>
- Observability: <…>
- Backward-compat: <…>
Verdict: <no Critical/High → safe to complete | N findings → fix then re-check>
```

Then run the §13 mechanical pass (consumer grep, wide pytest, ruff/build, diff
audit, fixture parity) and report both before `task_complete`.

## When a new recurring contract is discovered

If the critique surfaces a project-specific contract that bit you (like the
reconciler `status="running"` rule), record it in `/memories/repo/` so the NEXT
session knows it up front instead of rediscovering it. Keep the entry to a few
lines: the contract, where it is enforced, and the failure symptom.
