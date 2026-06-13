---
title: BLAST submit "no output captured" — elastic-blast missing from terminal sidecar
description: Root-cause fix for silent BLAST submit failures plus detailed failure logging in the exec server and submit task.
tags:
  - blast
  - terminal
---

# BLAST submit failed with "no output captured" — root cause + observability

## Motivation

A BLAST submit (job `92a9f152-…`, blastn / core_nt on `elb-cluster-01`) failed
after ~3 s with the banner **"elastic-blast submit failed (no output
captured)."** No error detail appeared anywhere: the Run details page showed an
opaque message and the worker container log had **no line** explaining the
failure.

## Root cause

The terminal sidecar's programmatic exec server could not spawn the
`elastic-blast` binary. The terminal sidecar audit log was the only place the
real error surfaced:

```
exec_error request_id=… error="[Errno 2] No such file or directory: 'elastic-blast'"
```

The deployed terminal image had the `elastic_blast` **package** installed under
`/opt/elb/venv` but **no `elastic-blast` executable** (`which elastic-blast` →
`None`, `/opt/elb/venv/bin/el*` empty, dist-info had no `entry_points.txt`).

The sibling `elastic-blast-azure` `setup.cfg` installs the CLI via
`scripts = bin/*`. Upstream commit `72a69822` ("Remove deprecated scripts")
deleted the `bin/` directory, so any build from `master` (or any ref at/after
that commit) installs the package but drops the launcher. The terminal base
build helper defaulted the build ref to **`master`**
(`scripts/dev/terminal-base-image.sh`), while the content hash and
`terminal/Dockerfile.base` `ARG` defaulted to the known-good pinned ref
`f4b8b734…` (which still ships `bin/`). That hash/build mismatch let a broken
image ship under a tag that looked correct.

Two compounding observability gaps turned a clear `FileNotFoundError` into a
silent failure:

1. The exec server's **streaming** endpoint sends HTTP 200 + headers *before*
   spawning the child. When `_spawn` raised, the exception escaped after the
   headers were already sent, so the client received an **empty body** (no
   NDJSON lines, no summary). The api caller then reported the generic "no
   output captured".
2. The submit task's non-retryable failure branch only wrote job state and
   returned — it emitted **no worker-log record**, so Log Analytics had nothing
   to grep by job id.

## User-facing change

- BLAST submits work again once the terminal base image is rebuilt from the
  pinned ref (the deploy step below). The image build now **fails loudly** if
  the `elastic-blast` launcher is missing, so a bad ref can never ship silently.
- When the exec server cannot start a binary, the failure is surfaced as a real
  stderr line + a non-zero (`127`) summary, so the Run details page shows the
  actionable diagnostic (e.g. `exec: cannot start 'elastic-blast': [Errno 2]
  No such file or directory`) instead of "no output captured".
- Non-retryable submit failures now emit a `blast_submit_failed` ERROR record
  in the worker log (job id, exit code, timeout flag, captured-output size,
  error snippet).

## API / IaC diff summary

- `scripts/dev/terminal-base-image.sh`: introduce `_ELASTIC_BLAST_REF_DEFAULT`
  (= `f4b8b734…`) and resolve the hash, log, and `az acr build --build-arg`
  through it. Removes the `:-master` build default that diverged from the hash.
- `terminal/Dockerfile.base`, `terminal/Dockerfile`: pin `ARG
  ELASTIC_BLAST_REF=f4b8b734…` (Dockerfile switched from `git clone --branch`
  to `git fetch <ref>` so a commit SHA resolves) and add a post-install
  `command -v elastic-blast || exit 1` build guard.
- `terminal/exec_server.py`: `_stream` catches the spawn `OSError` and emits a
  stderr line + `{"exit_code": 127, "error": …}` summary instead of letting the
  exception escape after headers are sent; the buffered-path 500 now carries the
  error `detail`.
- `api/tasks/blast/submit_task.py`: add a `LOGGER.error("blast_submit_failed …")`
  record in the non-retryable submit-failure branch.

## Validation evidence

- New tests: `api/tests/test_terminal_exec_server.py` (spawn-failure diagnostic
  contract + static ref-pin / build-guard checks) and
  `test_blast_submit_capacity_gate.py::test_submit_failed_logs_diagnostic_for_missing_elastic_blast`.
- `uv run pytest -q api/tests` → 3492 passed, 3 skipped.
- `uv run ruff check api` → clean. `bash -n scripts/dev/terminal-base-image.sh` → OK.
- Root cause confirmed in prod via the terminal sidecar audit log
  (`exec_error … No such file or directory: 'elastic-blast'`) and a live exec
  into the sidecar (`which elastic-blast` → `None`).

## Deploy step required to fix the live system

The fix takes effect only after the terminal base image is rebuilt from the
pinned ref:

```bash
ELASTIC_BLAST_REF=f4b8b734a82285a18a2ca9aadcbe02759d13f903 \
  scripts/dev/quick-deploy.sh terminal --rebuild-terminal-base
```
