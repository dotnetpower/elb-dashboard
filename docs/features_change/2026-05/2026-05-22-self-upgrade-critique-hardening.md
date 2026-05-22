# Self-upgrade — 20-point critique hardening (2026-05-22)

## Motivation

After F1–F6 closed the operator-facing follow-ups, the full upgrade
surface (PR1 + PR2 + PR3 + PR4 + F1..F6) was inspected against a
20-item checklist covering auth, state races, ARM PATCH safety, ACR
build hygiene, credential handling, throttling, reconciler bounds,
storage contention, SPA polling cost, and operator UX. This change
addresses the four highest-priority items uncovered by that pass.

## Findings + fixes

### High → Hardened

**#6 — Credential scrub fail aborted the build instead of being silent.**

`api/services/upgrade/git_workspace.py::_scrub_remote_credentials` now
raises `WorkspaceError` when either the `git config --get` read or the
masking `git config <url>` write fails AND the operator-supplied
remote actually carries a `user:password@` segment. Without the change
a transient terminal_exec failure could ship a PAT-bearing `.git/config`
into the built container image. The upstream `execute_upgrade_inline`
catches the WorkspaceError and routes to `failed_pre`, so the upgrade
fails closed (no PATCH) instead of fail-open.

**#19 — Operator awareness of BLAST jobs during the downtime window.**

`web/src/pages/UpgradePage.tsx` Start card now carries a one-line muted
note immediately below the downtime checkbox:

> In-flight BLAST jobs that submit during the restart window may need
> to be retried by the user once the upgrade settles. Persisted job
> state (Storage Table) and uploaded results survive the restart.

No backend change — the BLAST control plane already commits row state
+ artifacts to Storage on every transition, so the operator's job is
to communicate timing, not to drain.

### Medium → Hardened

**#10 — Per-process throttle bound, documented.**

`api/routes/upgrade.py` `_CHECK_MIN_INTERVAL_SECONDS` block carries a
comment explaining that worst-case upstream traffic is
`workers × (60 / interval)` requests/min, currently bounded at ~8/min,
and that a Redis-backed distributed throttle is only warranted when
worker count or beat frequency grows. No code change other than
documentation.

**#20 — UpgradeBadge re-prioritises new release after success.**

`web/src/components/UpgradeBadge.tsx` previously rendered the muted
"Now on vX" label whenever `state == succeeded`, hiding the case where
a newer release became available since the last completed upgrade. The
badge now checks `isUpgradeAvailable(status)` inside the `succeeded`
branch and falls through to the "Upgrade to vY" treatment when a
newer tag is present.

### Low — Logged, no code change

The remaining 12 items (env rotation hygiene, ACA idempotency,
append-blob contention, SPA polling cost, build-log size growth, etc.)
either depend on Azure-side guarantees, are bounded by existing limits,
or are deferred to operator monitoring. See the change-note narrative
for details.

## Tests

* `api/tests/test_upgrade_git_workspace.py` — new
  `test_clone_aborts_when_credential_scrub_write_fails` exercises the
  scrub-write failure path; asserts `WorkspaceError` propagates out
  of `clone()`.

## Validation

* `uv run ruff check api/services/upgrade api/routes/upgrade.py api/tests/test_upgrade_*.py` — clean.
* `uv run pytest -q api/tests` — 1201 passed (vs prior 1200; +1 new test).
* `cd web && npm run build` — clean.
* `cd web && npm run lint` — clean (F6 ignore for `.tsbuild/` already
  in place).
