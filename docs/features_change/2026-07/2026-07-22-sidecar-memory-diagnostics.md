---
title: Sidecar memory — deep investigation + opt-in api memory diagnostics (Phase 0)
description: Investigate the suspected api-sidecar memory leak, conclude there is no unbounded module-level growth (all caches/clients/threads are bounded) but two structural gaps (no memory-recycle backstop on the single long-lived api process, and zero leak-vs-plateau instrumentation), and ship a default-OFF memory sampler + malloc_trim mitigation as the safe first step of a phased plan.
tags:
  - operate
  - security
---

# Sidecar memory — deep investigation + api memory diagnostics (Phase 0)

## Motivation

A follow-up to the earlier six-sidecar audit asked to deep-investigate a
*suspected* memory leak in the sidecars and produce an improvement plan.

### Investigation findings

Re-auditing the runtime leak vectors (not just static module globals) across the
`api` and `worker` sidecars found **no smoking-gun unbounded growth**:

- Every module-level cache is bounded — JWKS/claims (auth), blob-service pool
  (LRU 32 + idle TTL), k8s session pool (32 + 300 s TTL), rate-limiter buckets
  (`_MAX_KEYS`), execution-admission `_MEMORY`, SSE/WebSocket tickets (30 s TTL
  + issue-time sweep), pricing cache (now size-capped), and the various
  `*_CACHE_MAX_ENTRIES` service caches.
- Per-request Azure SDK / `httpx` clients are context-managed or closed in
  `finally`; the diagnostics `ThreadPoolExecutor` shuts down in `finally`; the
  SSE broadcaster and its `asyncio` task are cancelled on unsubscribe/shutdown;
  OpenTelemetry init is one-shot; streaming upload/download buffers are bounded
  (1 MiB / 4 MiB × 4 semaphore) and released per request.

Instead the "looks like a leak" symptom is most plausibly explained by **two
structural gaps**, not an actual leak:

1. **No memory-recycle backstop on the api sidecar.** The `worker` recycles
   prefork children by task count (200) and resident memory (~244 MiB), so any
   slow growth there resets regularly. The `api` sidecar is a **single
   `--workers 1` uvicorn process** with no equivalent — a slow climb or a high
   plateau is never reset until the container OOM-restarts.
2. **Zero leak-vs-plateau instrumentation.** There is no `tracemalloc`, GC-stats,
   `malloc_trim`, or RSS-trend logging anywhere in `api/`, so a rising RSS
   cannot be distinguished from a bounded plateau. glibc also keeps freed arenas
   resident (it does not return them to the OS by default), so a transient spike
   (e.g. a burst of 4 MiB streaming blocks) can leave RSS permanently high and
   *look* monotonic even when the Python heap is flat.

Fixing "the leak" blind would be guesswork. The disciplined first step is to
make the process **measurable**, then act on evidence.

## User-facing change

None by default. A new **opt-in** memory sampler is available for operators
investigating a suspected leak on the deployed `api` sidecar. It is entirely
default-OFF (zero cost when unset) and enabled per-investigation via env vars on
an `az containerapp update` — no redeploy of code required:

- `API_MEMTRACE_INTERVAL_SECONDS=<seconds>` — enable; periodically logs
  `memtrace rss=… gc_count=… gc_objects=…` (clamped to a 5 s floor).
- `API_MEMTRACE_TRACEMALLOC=1` (+ `API_MEMTRACE_TOPN`, `API_MEMTRACE_FRAMES`) —
  also log the top-N allocation sources (adds tracking overhead; separately
  gated).
- `API_MALLOC_TRIM=1` — call `malloc_trim(0)` after each sample to hand freed
  glibc arenas back to the OS, and log the reclaimed RSS delta. This doubles as
  a **mitigation** if the "leak" turns out to be glibc arena retention.

## API / IaC diff summary

- `api/app/memory_diagnostics.py` (new) — stdlib-only sampler: `read_rss_bytes`
  (from `/proc/self/status`), `sample_once` (RSS + GC counts + object count +
  optional tracemalloc top-N + optional trim), `malloc_trim` (best-effort via
  `ctypes`, never raises), and `start_memory_sampler` (default-OFF daemon
  thread, defensive env parsing, never raises out of its loop).
- `api/app/lifespan.py` — start the sampler before `yield`, signal its stop
  event in the shutdown `finally` (stored on `app.state._memtrace_stop`).
- `api/tests/test_memory_diagnostics.py` (new) — 8 tests: RSS read, sample
  shape + log line, trim delta, `malloc_trim` never raises, default-OFF,
  invalid-interval-OFF, enabled start/stop, env clamp/fallback.

No Bicep change: the sampler reads env vars directly, so an operator sets them
on the running Container App only while investigating and removes them after.

## Phased improvement plan

- **Phase 0 (this change)** — instrumentation + `malloc_trim` mitigation, all
  default-OFF. Turn it on when a leak is suspected to get evidence.
- **Phase 1** — if evidence shows glibc arena retention (RSS high but
  `gc_objects` flat and `tracemalloc` flat), enable `API_MALLOC_TRIM=1`
  permanently, or set `MALLOC_TRIM_THRESHOLD_`/`MALLOC_ARENA_MAX` on the api
  container to bound arena growth. Cheap, reversible, no code.
- **Phase 2** — if evidence shows a genuine Python-object leak, use the
  `tracemalloc` top-N to pin the source and fix the specific retention
  (a cache miss on the bound, a lingering reference), then add a regression
  test that asserts the object count stays flat across N iterations.
- **Phase 3 (only if 1–2 are insufficient)** — give the api sidecar a
  memory-recycle backstop symmetric with the worker: an uvicorn
  `--limit-max-requests` recycle or a supervised RSS watchdog that restarts the
  process above a high-water mark. Deferred because it is a heavier change and
  Container Apps already OOM-restarts as the ultimate backstop.

## Validation evidence

- `uv run pytest -q api/tests/test_memory_diagnostics.py` → 8 passed.
- `uv run pytest -q api/tests/test_smoke.py -k "readiness or health or startup
  or lifespan"` → 13 passed (lifespan hook does not regress startup/shutdown).
- `SIDECAR_REPORTER_DISABLED=true AUTH_DEV_BYPASS=true python -c "import
  api.main"` → app imports cleanly, "api sidecar started".
- `uv run ruff check api/app/memory_diagnostics.py
  api/tests/test_memory_diagnostics.py api/app/lifespan.py` → all checks passed.
