# Local development log sessions

## Motivation

Local debugging previously depended on terminal scrollback or ad-hoc commands
such as `tee /tmp/api.log`. That made warnings, errors, and pipeline health hard
to review after the terminal was closed, and `/tmp` logs were outside the
workspace.

## User-facing change

VS Code local development tasks now mirror stdout/stderr into project-local log
sessions under `.logs/local/`:

```text
.logs/local/
  latest -> 20260515T143012Z-12345
  20260515T143012Z-12345/
    api.log
    worker.log
    beat.log
    web.log
    redis.log
    smoke.log
    compose-full.log
    compose-full-containers.log
```

The wrapper keeps console output unchanged, so task readiness matchers and
terminal feedback still work. The newest 3 sessions are retained, and each log
chunk is capped at 1 MiB by default. To keep logging from becoming a local
bottleneck, each service log is a bounded 16-chunk ring, file flushes are
batched after the initial header lines, and detached Docker Compose followers
tail only the newest 200 container-log lines before following. High-volume runs
can also set `LOCAL_LOG_CONSOLE=false` to avoid terminal rendering overhead
while still writing project-local files. Follow-up hardening rejects unsafe
session names, recovers stale log locks, prunes stale chunks above the active
chunk cap, and cleans orphaned detached compose log followers by compose
profile.

Direct terminal launches use the same log path through
`scripts/dev/local-run.sh <api|worker|beat|web|redis|smoke|compose-full|compose-local>`,
so agents and humans get logs even when they do not start processes through VS
Code tasks. Docker Compose runs get both command logs and, for detached `up -d`,
a background container-log follower.

## API / script diff summary

* Added `scripts/dev/run-with-log.sh`.
  * Creates/reuses a fresh local session under `.logs/local/`.
  * Updates `.logs/local/latest` to the active session.
  * Keeps the newest 3 sessions by default.
  * Caps each log chunk at `LOCAL_LOG_MAX_BYTES=1048576` by default.
  * Caps each service stream at `LOCAL_LOG_MAX_CHUNKS=16` chunks per session.
  * Flushes the first lines immediately, then batches file flushes with
    `LOCAL_LOG_FLUSH_LINES=50` by default.
  * Supports `LOCAL_LOG_CONSOLE=false` to disable console mirroring when the
    terminal itself is the slow path.
  * Supports `LOCAL_LOG_SESSION` for a forced shared session.
  * Rejects unsafe session names before filesystem use.
  * Recovers stale lock directories with `LOCAL_LOG_LOCK_STALE_SECONDS=30`.
  * Prunes stale chunk files above the active `LOCAL_LOG_MAX_CHUNKS` cap.
* Added `scripts/dev/local-run.sh`.
  * Provides direct terminal commands for `api`, `worker`, `beat`, `web`,
    `redis`, `smoke`, `compose-full`, and `compose-local`.
  * Sets the same local defaults the VS Code tasks need, then delegates to
    `run-with-log.sh`.
* Added `scripts/dev/compose-with-log.sh`.
  * Wraps Docker Compose foreground output into `compose-<profile>.log`.
  * Starts a background `docker compose logs -f --no-color` follower for
    detached `up -d`, writing `compose-<profile>-containers.log`.
  * Bounds detached history replay with `COMPOSE_LOG_TAIL=200` by default.
  * Stops stale followers for the same compose profile before starting a new
    detached follower, and on `down`, `stop`, or `rm`.
* Updated `.vscode/tasks.json`.
  * `redis: ensure`, `api: start`, `worker: start`, `beat: start`, `web: dev`,
    and `smoke: api` now run through `local-run.sh`.
* Updated docs.
  * README and `scripts/dev/README.md` describe where to find logs and the
    retention/chunking rules.
* Updated `.gitignore`.
  * `.logs/` is ignored.

## Validation evidence

```
$ cd /home/moonchoi/dev/elb-dashboard && bash -n scripts/dev/compose-with-log.sh scripts/dev/local-run.sh scripts/dev/run-with-log.sh
exit=0

$ cd /home/moonchoi/dev/elb-dashboard && python3 -m json.tool .vscode/tasks.json >/dev/null
exit=0

$ cd /home/moonchoi/dev/elb-dashboard && rm -rf .logs/local/validation && LOCAL_LOG_SESSION=validation LOCAL_LOG_MAX_BYTES=1024 scripts/dev/run-with-log.sh api -- bash -lc 'python3 - <<"PY"
print("x" * 1500)
PY' >/tmp/elb-log-smoke.out && find .logs/local/validation -maxdepth 1 -type f -printf '%f %s\n' | sort
api.log 1024
api.log.1 690

$ cd /home/moonchoi/dev/elb-dashboard && rm -rf .logs/local/ring-cap-test && LOCAL_LOG_SESSION=ring-cap-test LOCAL_LOG_MAX_BYTES=1024 LOCAL_LOG_MAX_CHUNKS=2 LOCAL_LOG_FLUSH_LINES=100 scripts/dev/run-with-log.sh api -- bash -lc 'python3 - <<"PY"
for i in range(20):
  print(f"line-{i:02d}-" + "x" * 240)
PY' >/tmp/elb-ring-cap.out && find .logs/local/ring-cap-test -maxdepth 1 -type f -printf '%f %s\n' | sort
api.log 1024
api.log.1 150

$ cd /home/moonchoi/dev/elb-dashboard && rm -rf .logs/local/console-off-test && LOCAL_LOG_SESSION=console-off-test LOCAL_LOG_CONSOLE=false scripts/dev/run-with-log.sh api -- bash -lc 'echo hidden-on-console' >/tmp/elb-console-off.out && test ! -s /tmp/elb-console-off.out && grep -q hidden-on-console .logs/local/console-off-test/api.log && wc -c /tmp/elb-console-off.out .logs/local/console-off-test/api.log
  0 /tmp/elb-console-off.out
256 .logs/local/console-off-test/api.log
256 total

$ cd /home/moonchoi/dev/elb-dashboard && set +e; LOCAL_LOG_SESSION='../bad' scripts/dev/run-with-log.sh api -- true >/tmp/elb-bad-session.out 2>&1; rc=$?; set -e; test "$rc" -eq 2 && grep -q unsafe /tmp/elb-bad-session.out
exit=0; unsafe session names are rejected.

$ cd /home/moonchoi/dev/elb-dashboard && rm -rf .logs/local/stale-lock-test .logs/local/.lock && mkdir -p .logs/local/.lock && touch -d '2 minutes ago' .logs/local/.lock && LOCAL_LOG_SESSION=stale-lock-test LOCAL_LOG_LOCK_STALE_SECONDS=1 scripts/dev/run-with-log.sh api -- true >/tmp/elb-stale-lock.out && test -s .logs/local/stale-lock-test/api.log && test ! -d .logs/local/.lock
exit=0; stale lock recovered.

$ cd /home/moonchoi/dev/elb-dashboard && rm -rf .logs/local/stale-chunk-test && mkdir -p .logs/local/stale-chunk-test && : > .logs/local/stale-chunk-test/api.log.99 && LOCAL_LOG_SESSION=stale-chunk-test LOCAL_LOG_MAX_CHUNKS=2 scripts/dev/run-with-log.sh api -- true >/tmp/elb-stale-chunk.out && test ! -e .logs/local/stale-chunk-test/api.log.99
exit=0; stale chunks above the active cap are removed.

$ cd /home/moonchoi/dev/elb-dashboard && rm -rf .logs/local/direct-api-help && LOCAL_LOG_SESSION=direct-api-help scripts/dev/local-run.sh api -- --help >/tmp/elb-local-run-api-help.out && test -s .logs/local/direct-api-help/api.log && grep -q 'Usage: uvicorn' .logs/local/direct-api-help/api.log
exit=0; direct terminal api launch wrote .logs/local/direct-api-help/api.log

$ cd /home/moonchoi/dev/elb-dashboard && LOCAL_LOG_SESSION=compose-config-test scripts/dev/local-run.sh compose-full -- config --services >/tmp/elb-compose-config.out && test -s .logs/local/compose-config-test/compose-full.log && grep -q '^api$' /tmp/elb-compose-config.out
exit=0; compose command output wrote compose-full.log

$ cd /home/moonchoi/dev/elb-dashboard && rm -rf .logs/local/compose-detached-test && LOCAL_LOG_SESSION=compose-detached-test scripts/dev/local-run.sh compose-full -- up -d --no-build terminal >/tmp/elb-compose-detached.out && test -s .logs/local/compose-detached-test/compose-full.log && test -s .logs/local/compose-detached-test/compose-full-containers.log && pid=$(cat .logs/local/.compose-full-containers.pid); kill "$pid" 2>/dev/null || true; rm -f .logs/local/.compose-full-containers.pid; find .logs/local/compose-detached-test -maxdepth 1 -type f -printf '%f %s\n' | sort
compose-full-containers.log 333
compose-full.log 378

$ cd /home/moonchoi/dev/elb-dashboard && rm -rf .logs/local/compose-tail-test && COMPOSE_LOG_TAIL=5 LOCAL_LOG_SESSION=compose-tail-test scripts/dev/local-run.sh compose-full -- up -d --no-build terminal >/tmp/elb-compose-tail.out && test -s .logs/local/compose-tail-test/compose-full-containers.log && grep -q -- '--tail 5' .logs/local/compose-tail-test/compose-full-containers.log; pid=$(cat .logs/local/.compose-full-containers.pid); kill "$pid" 2>/dev/null || true; rm -f .logs/local/.compose-full-containers.pid
exit=0; detached compose follower used bounded replay and wrote an immediate header.

$ cd /home/moonchoi/dev/elb-dashboard && LOCAL_LOG_SESSION=follower-cleanup-test COMPOSE_LOG_TAIL=5 scripts/dev/local-run.sh compose-full -- up -d --no-build terminal >/tmp/elb-follower-cleanup.out && pid=$(cat .logs/local/.compose-full-containers.pid); test -n "$pid" && kill -0 "$pid" && kill -TERM -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true; rm -f .logs/local/.compose-full-containers.pid; ps -ef | grep -E 'docker compose -p elb-control-local .* logs -f|run-with-log.sh compose-full-containers' | grep -v grep || true
exit=0; stale compose log followers were cleaned up.

$ cd /home/moonchoi/dev/elb-dashboard && LOCAL_LOG_SESSION=retention-check scripts/dev/run-with-log.sh smoke -- true >/tmp/elb-log-retention.out
exit=0; retention cleanup kept the newest 3 session directories.
```
