# 2026-05-27 — Local log layout: one file per service, no session folders

## Motivation

`scripts/dev/local-run.sh` historically created a new timestamped session
directory under `.logs/local/` for every invocation (`20260527T072429Z-512422/`,
`20260527T073229Z-519660/`, `web-debug/`, `log-guarantee-*/`,
`web-detached-ci-test-*/`, …) and pointed a `latest -> <session>` symlink at
the most recent one. The `LOCAL_LOG_SESSION_TTL_SECONDS=120` reuse logic only
kept services together when they were started within the same 2-minute window;
ad-hoc later runs of `local-run.sh api` created a *new* session, moved the
symlink, and orphaned the previous logs in a sibling directory. The Live Wall
api routes (`api/services/sidecar_logs.py`) read from
`.logs/local/latest/<service>.log` — which silently snapped to whichever
session was most recent, so the dashboard's tail and a manually opened log
file frequently disagreed about which run was "current".

User feedback summarised it: "로컬에서 실행할때 로그가 엉망이야.. 어디에 남는지
찾지를 못하겠어. 로그 파일을 한개로 단순화 하자."

## User-facing change

* Logs always land in **one fixed location**:
  `.logs/local/latest/<service>.log` (a real directory now, not a symlink).
* One file per service across runs (`api.log`, `worker.log`, `beat.log`,
  `web.log`, `redis.log`, `terminal-exec.log`, `compose-*.log`,
  `<service>.launch.log` / `<service>.launch.pid` for detached starts).
* Appended across runs so a `restart` does not lose the previous traceback.
* Bounded ring rotation per service (`1 MiB × 5 chunks` by default = ≤ ~5 MiB
  on disk per service no matter how long you debug).
* New diagnostic helpers:
  * `scripts/dev/local-run.sh logs` — `ls -lah` of `.logs/local/latest/`.
  * `scripts/dev/local-run.sh logs-clean` — archive everything currently
    under `latest/` into `.logs/local/_archive/<utc-ts>/`.
* `scripts/dev/local-run.sh start` performs a **one-shot migration**: every
  leftover artifact from the retired session-folder layout (timestamped
  session dirs, `web-debug`, `log-guarantee-*`, `web-detached-ci-test-*`,
  `.current-session`, `.lock/`, …) is moved into
  `.logs/local/_archive/<utc-ts>/`. Nothing is deleted; stable state files
  (`api-<port>.lock`, `web.launch.pid`, `state/`, `deploy-*.log`) are kept
  in place.

Removed concepts (no replacement needed):

* `LOCAL_LOG_SESSION`, `LOCAL_LOG_SESSION_TTL_SECONDS`,
  `LOCAL_LOG_LOCK_STALE_SECONDS`, `LOCAL_LOG_KEEP_SESSIONS` env vars.
* `.logs/local/.current-session` marker, `.logs/local/.lock/` advisory lock,
  per-session `.active.<svc>.<pid>` markers, `cleanup_old_sessions()` /
  `select_session()` logic, `new_local_log_session()` propagation through
  detached service starts, `latest` symlink.

## API / IaC diff summary

No api, frontend, or infra code changed. Modified scripts only:

* `scripts/dev/run-with-log.sh` — full rewrite. Drops the session locking +
  selection layer entirely. Now writes directly to
  `${LOCAL_LOG_BASE}/latest/<service>.log`, resumes appending into the
  highest existing chunk on the next start, keeps the bounded ring (default
  reduced from 16 → 5 chunks). The awk-based mirroring + console tee and
  signal handling are preserved as-is.
* `scripts/dev/local-run.sh` — replaces `new_local_log_session()` with two
  small helpers: `ensure_log_dir()` (idempotent migration of `latest`
  symlink → real directory) and `prune_legacy_log_layout()` (one-shot
  migration of legacy entries into `_archive/<ts>/`). `start_detached_service`
  no longer threads a session name through `nohup env`. `run_server_start`
  prints the new single-file path (`tail -f .logs/local/latest/api.log`).
  Adds `logs` and `logs-clean` subcommands.
* `scripts/dev/compose-with-log.sh` — `start_detached_log_follower` resolves
  `latest_dir` as the literal directory now that `latest` is no longer a
  symlink (drops `readlink -f`).
* `scripts/dev/README.md` — rewrote the **Local logs** section to describe
  the new single-file layout, the kept env vars (`LOCAL_LOG_MAX_BYTES`,
  `LOCAL_LOG_MAX_CHUNKS`, `LOCAL_LOG_FLUSH_LINES`, `LOCAL_LOG_CONSOLE`,
  `COMPOSE_LOG_TAIL`), and the new `logs` / `logs-clean` subcommands.

`api/services/sidecar_logs.py` continues to read
`<log_base>/latest/<container>.log`, so the Live Wall contract is unchanged
(the existing `api/tests/test_sidecar_logs.py` suite covers this).

## Validation evidence

* Unit + smoke logging behaviour against a tmp base:
  * `LOCAL_LOG_BASE=/tmp/elb-log-test run-with-log.sh demo -- bash -c '…'`
    writes `/tmp/elb-log-test/latest/demo.log` only. Second invocation
    appends to the same file (no new directory).
  * Ring rotation with `LOCAL_LOG_MAX_BYTES=2048 LOCAL_LOG_MAX_CHUNKS=3`
    produces `rot.log` + `rot.log.1` + `rot.log.2`, latest content in
    `rot.log`, oldest pruned. Verified by tailing first/last lines.
* `uv run pytest -q api/tests` → **1505 passed in 26.83s** (no regressions).
* `uv run pytest -q api/tests/test_sidecar_logs.py` → 6 passed (Live Wall
  contract still satisfied with the new `latest/` directory).
* Live end-to-end: `scripts/dev/local-run.sh stop` → `start` archived 16
  legacy entries into `.logs/local/_archive/20260527T075152Z/` and produced
  exactly one populated directory `.logs/local/latest/` containing
  `{api,worker,beat,web,redis,terminal-exec}.log` plus matching `.launch.log`
  / `.launch.pid` files. `curl http://127.0.0.1:8085/api/health` returned
  `{"status":"ok","version":"0.2.0",…}`.
* `scripts/dev/local-run.sh logs` lists the new directory cleanly with
  `ls -lah`.
* `bash -n` clean on all three modified scripts.

## Self-review

* Consumers of `LOCAL_LOG_SESSION` env: only `scripts/dev/run-with-log.sh`
  (now removed) and `scripts/dev/local-run.sh` (now removed). Remaining
  matches are inside historical change notes under `docs/features_change/`
  — intentionally left as-is (they describe past behaviour).
* `api/services/sidecar_logs.py` and `api/tests/test_sidecar_logs.py`
  contract is `<log_base>/latest/<container>.log` — preserved.
* `scripts/dev/compose-with-log.sh` `readlink -f` replaced with a plain
  directory reference; downstream `target_log` path is unchanged.
* `web/`, `infra/`, `api/`, and `terminal/` sources untouched.
* `git diff --stat` shows only the four expected files modified
  (`scripts/dev/run-with-log.sh`, `scripts/dev/local-run.sh`,
  `scripts/dev/compose-with-log.sh`, `scripts/dev/README.md`) plus this
  change note.
