# cli-upgrade hardening round 2 — concurrent-deploy lock, deploy history, richer probe diagnostics

## Motivation
Round 1 of the cli-upgrade deep-health work (PR series ending
`72098a7`) closed the silent-Storage-failure footgun. Critique then
surfaced three "deferred" items: a probe diagnostic field, an
operator-friendly snapshot race condition, and missing deploy-side
observability. This change addresses all three in one PR rather than
fragmenting across follow-ups.

## User-facing change
Three independent additions, all opt-in / additive:

1. **Richer Storage probe diagnostics.** When `/api/health/ready`
   reports `azure_storage: down`, the response body now also includes
   `error_class` (e.g. `HttpResponseError`, `ServiceRequestError`,
   `ClientAuthenticationError`, `TimeoutError`). Operators can map the
   class to a 1st-line action without parsing the free-form `error`
   string. The `error` field itself is unchanged for back-compat with
   any existing parsers.

2. **Concurrent-deploy lockout.** `cli-upgrade.sh` now takes an
   exclusive `flock(2)` on `/tmp/elb-upgrade-snapshot-<app>.json.lock`
   before reading or writing the snapshot. Two operators racing
   `cli-upgrade.sh full` against the same Container App can no longer
   corrupt the rollback snapshot (the second run is rejected with a
   clear error pointing at the lockfile). The lock is released on
   normal exit, `die`, Ctrl+C, or SIGTERM via `set -E` trap behavior.

3. **Deploy history.** Every terminal outcome of `cli-upgrade.sh`
   appends one JSON line to `$ELB_UPGRADE_HISTORY` (default
   `~/.elb-upgrade-history.jsonl`) with `ts`, `scope`, `app`, `tag`,
   `head_sha`, `result`, `elapsed_seconds`, `message`. Implemented via
   a single EXIT trap so every exit path (success, parity rejection,
   build failure, rollback, Ctrl+C, internal error) gets recorded
   exactly once, without scattering log calls across the script.

## API / IaC diff
* `api/routes/health.py`
  * `_probe_storage_table()` augments the `down` payload with
    `error_class: type(exc).__name__`. The existing `error: str(exc)[:200]`
    field is unchanged.
* `api/tests/test_smoke.py`
  * Narrowed the `_reset_storage_probe_cache_between_tests` autouse
    fixture so it only runs for `test_readiness_storage*` tests, not
    for the entire file. Saves a couple hundred microseconds per
    unrelated test and clarifies the fixture's actual scope.
  * New `test_readiness_storage_down_payload_includes_error_class`
    pins the additive field contract.
* `scripts/dev/cli-upgrade.sh`
  * `take_snapshot` and `restore_from_snapshot` are now wrapped in an
    advisory file lock acquired at script start via `exec 9>...; flock -n 9`.
    Second concurrent run dies with a recoverable error.
  * New `record_history()` + `set_result()` + EXIT trap that writes one
    JSONL line to `$ELB_UPGRADE_HISTORY`. Outcomes covered: `success`,
    `parity_rejected`, `build_in_progress`, `upgrade_failed_rolled_back`,
    `rollback_failed`, `rollback_success`, `aborted_by_user`,
    `aborted`. Dry-run and `--help` are intentionally excluded.
* `docs/operate/cli-upgrade.md`
  * Two new "Common failure modes" rows: concurrent-deploy lock error
    and the new `error_class` diagnostic hint.
  * New "Deploy history" section with format, result values, and three
    jq one-liners (recent runs, outcome counts, average elapsed).

No Bicep, Container App template, or response-shape changes for any
existing client. The new `error_class` is additive.

## Validation
* `uv run pytest -q api/tests/test_smoke.py -k readiness_storage`
  → 6 passed (existing 5 + new error_class case).
* `uv run ruff check api/routes/health.py api/tests/test_smoke.py`
  → All checks passed.
* `bash -n scripts/dev/cli-upgrade.sh` → syntax OK.
* End-to-end against deployed `ca-elb-dashboard`:
  * Two concurrent `cli-upgrade.sh full --allow-dirty --dry-run`
    invocations → first succeeds, second exits 1 with
    `another cli-upgrade run holds /tmp/elb-upgrade-snapshot-...lock`.
  * History file populated with one entry per run; `--help` and
    dry-run produce no entries as designed.
  * Bad scope (`cli-upgrade.sh xxxxxxx`) records one `aborted` entry
    with `exit=1`, scope=`unknown`.

## Operator note
The `cli-upgrade.sh` lockfile lives at
`/tmp/elb-upgrade-snapshot-<CONTAINER_APP_NAME>.json.lock`. If a
previous run was force-killed (kill -9) before the kernel released
the `flock`, the lockfile may persist as stale. Remove it manually
when no `cli-upgrade.sh` process is alive:

```bash
pgrep -f cli-upgrade.sh
rm /tmp/elb-upgrade-snapshot-<app>.json.lock
```

The history file is per-host, so deploys from a CI runner vs a
developer workstation will not appear in the same jsonl. For
cross-host aggregation, redirect `ELB_UPGRADE_HISTORY` to a shared
location (Azure Files mount, SMB share, etc.) — at your own risk
since the atomicity guarantee relies on local-fs `O_APPEND` semantics.
