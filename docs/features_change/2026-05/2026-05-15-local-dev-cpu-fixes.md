# Local-dev CPU usage drop (uvicorn reloader 34% → ~0%)

**Date**: 2026-05-15
**Scope**: local developer experience only — no production / Container App impact.

## Motivation

Running `api: start` (uvicorn `--reload`) on WSL2 burned ~34–40% of one core
**at idle**, plus filled `/tmp/api.log` at multi-MB/min. The dashboard's TanStack
Query polling on top of that pushed the laptop fan to spin constantly during a
plain editor session. We needed to find the actual CPU sink, not just guess.

## Diagnosis

1. `top` on the uvicorn process: **34% CPU idle** with `--reload`,
   **1.0% CPU idle** without `--reload` → reloader is the culprit.
2. `/proc/<pid>/fdinfo/*` had **zero inotify watches** → `watchfiles` was in
   polling mode, scanning the workspace tree on every tick.
3. `py-spy dump` showed the main thread parked in
   `watchfiles/main.py:130 watch()` (the Rust polling loop).
4. Read [.venv/lib/python3.12/site-packages/uvicorn/supervisors/watchfilesreload.py](../../../.venv/lib/python3.12/site-packages/uvicorn/supervisors/watchfilesreload.py#L65-L69)
   and found:

   ```python
   if Path.cwd() not in self.reload_dirs:
       self.reload_dirs.append(Path.cwd())
   ```

   uvicorn 0.32.0 **forcibly appends `cwd` to `--reload-dir`** unless cwd is
   already in the list. So `--reload-dir api` from the workspace root was
   silently equivalent to `--reload-dir api --reload-dir <workspace_root>`,
   and watchfiles was walking `.venv/`, `web/node_modules/`, `.ruff_cache/`,
   `.benchmarks/`, etc. on every poll.

The two LOG_LEVEL fixes below are independent of the CPU fix — both came up
during the same investigation because Azure SDK at DEBUG dumps full HTTP
request/response headers per call (huge log volume + measurable CPU).

## User-facing change

| What                                       | Before                                  | After                          |
| ------------------------------------------ | --------------------------------------- | ------------------------------ |
| `api: start` task idle CPU                 | **34–40%** of one core                  | **~0%** (inotify-driven)       |
| `worker: start` / `beat: start` log volume | DEBUG dumps every Azure SDK HTTP call   | INFO; HTTP details only on warn|
| Auto-open helper API calls per dashboard poll | 1× ARM `get_properties` + 1× `ipify` GET per `/api/blast/databases` request | Cached for 60 s in-process |

No behaviour change for end users. Local dev experience only.

## API / IaC diff summary

* [.vscode/tasks.json](../../../.vscode/tasks.json) — `api: start`:
  * `cwd` → `${workspaceFolder}/api` (was `${workspaceFolder}`)
  * `--reload-dir .` (was `--reload-dir api`)
  * Together: cwd matches the requested reload dir, so uvicorn's auto-cwd-append
    becomes a no-op and watchfiles only scans `api/`.
  * `LOG_LEVEL` → `INFO` (was `DEBUG`).
  * Added `LOCAL_DEBUG_AUTO_OPEN_STORAGE: "true"` (separate change, kept for completeness).
* [.vscode/tasks.json](../../../.vscode/tasks.json) — `worker: start` and
  `beat: start`: `LOG_LEVEL` → `INFO` (was `DEBUG`).
* [api/main.py](../../../api/main.py) — after `logging.basicConfig(...)`,
  silence noisy third-party loggers regardless of `LOG_LEVEL`:
  `azure.core.pipeline.policies.http_logging_policy`, `azure.identity` family,
  `urllib3.connectionpool`, `httpx`, `watchfiles`. Defaults to `WARNING`.
  Override via `AZURE_LOG_LEVEL=DEBUG` when wire-level traces are needed.
* [api/services/storage_public_access.py](../../../api/services/storage_public_access.py) —
  added a 60 s in-process TTL cache (`_already_open_cache`, `threading.Lock`) so
  repeated `/api/blast/databases` polls don't fire ARM `get_properties` + `ipify`
  GET per request. First call costs both; next 60 s reuse the verdict.
* [api/tests/test_storage_public_access.py](../../../api/tests/test_storage_public_access.py) —
  added `test_ensure_already_open_is_cached`; updated autouse fixture to clear
  the cache between tests.

No production code path changes. Container App env never sets
`LOCAL_DEBUG_AUTO_OPEN_STORAGE`, never runs `--reload`, and `LOG_LEVEL`
is unaffected for prod.

## Validation evidence

```
# Before fix (--reload-dir api, cwd=workspace root)
top -b -n 2 -d 1 -p <uvicorn-pid> | tail -3
# 125218 ... S 34.0 0.1 ... uvicorn

# After fix (--reload-dir ., cwd=api/)
top -b -n 2 -d 1 -p <uvicorn-pid> | tail -3
# 128792 ... S  0.0 0.1 ... uvicorn

# Confirm inotify (zero watches before, present after — WSL2 ext4 supports it)
for fd in /proc/<pid>/fdinfo/*; do grep -l '^inotify' "$fd"; done
```

Tests:
```
uv run pytest -q api/tests/test_storage_public_access.py
# 16 passed in 0.49s
uv run ruff check api/services/storage_public_access.py api/main.py
# All checks passed!
```

## Why this is the right fix

* **Doesn't fight uvicorn** — works *with* uvicorn 0.32.0's auto-cwd-append by
  giving it a `cwd` that already matches the desired watch root. Won't break
  on uvicorn upgrades.
* **No new dependency, no daemon flags** — purely a `tasks.json` + `cwd`
  rearrangement plus an opt-out env var for log verbosity.
* **Reversible** — set `AZURE_LOG_LEVEL=DEBUG` in env to get the previous
  wire-level Azure logs back when actually debugging an Azure SDK call.

## Out of scope

* Vite / web dev server CPU (separate node tree, ~62% combined). Not touched
  here; user can address via `web/` task changes if needed.
* Production `prod` mode never uses `--reload`, so this change has zero effect
  on the deployed Container App.
