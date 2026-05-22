# exec_server: line-length cap + temp-dir GC daemon

## Motivation
Two latent unbounded growth paths in `terminal/exec_server.py`:

* `_stream._read_pipe` called `pipe.readline()` with no length limit. A
  child that prints a single line of hundreds of MB (binary blob,
  `az --debug` with no embedded newlines, …) would let one bytearray
  grow until the sidecar OOMs — even though the per-request output cap
  added for `_run_buffered` would have caught the buffered case.
* `_make_cwd` created a fresh `mkdtemp` under `EXEC_TMP_ROOT` for every
  request. The per-request `finally` cleans up on the happy path, but a
  SIGKILL (revision rollover, OOMKill, OS hangup) leaves the dir behind
  and `/tmp/exec` slowly fills the ephemeral disk over hours of BLAST
  traffic.

## User-facing change
None directly. Streaming responses no longer truncate at line-break in
the cap-overflow case — they emit the captured prefix followed by a
` [truncated:line-over-cap]` marker so the dashboard renders the
truncation explicitly. Disk usage on the terminal sidecar stays bounded.

## API / IaC diff
* `terminal/exec_server.py`
  * `NDJSON_LINE_MAX_BYTES` (default 64 KiB, env
    `EXEC_STREAM_LINE_MAX_BYTES`). `_read_pipe` switches to
    `pipe.readline(cap + 1)` and drains any over-cap line up to the next
    `\n` so the next read starts cleanly. The line emitted to the
    NDJSON stream is the captured prefix + the truncation marker.
  * `EXEC_TMPDIR_GC_INTERVAL_SECONDS` (default 300 s, env-overridable)
    and `EXEC_TMPDIR_GC_MAX_AGE_SECONDS` (default 3600 s) drive a new
    `_gc_stale_tmpdirs(max_age_seconds)` sweep that removes
    `EXEC_TMP_ROOT/req-*` dirs older than the cap. A startup sweep
    catches anything left behind by the previous container instance.
  * `_start_tmpdir_gc_thread()` spawned from `main()`.

## Validation
* `uv run pytest -q api/tests/test_terminal_exec.py` — 15 passed (line
  cap + buffered output cap + concurrency tests unchanged).
* `uv run ruff check terminal/exec_server.py` — clean.
