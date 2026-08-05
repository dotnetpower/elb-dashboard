---
title: Stop the api sidecar OOM loop by returning freed glibc arenas
description: The api container was SIGKILL'd (exit 137) up to 56 times a day because glibc retained 40-47% of RSS as freed-but-unreturned arenas after large blob downloads and XML/JSON parsing. Add an always-on periodic malloc_trim so RSS stays flat instead of ratcheting to the 2 GiB limit.
tags: [operate, infra]
---

# Stop the api sidecar OOM loop by returning freed glibc arenas

## Motivation

The Dashboard's Sidecar pressure card showed `api` at **92 % of 2 Gi** with a
`WATCH` badge. That was not a display glitch — the api container was being
**OOM-killed and silently restarted**:

```
2026-08-05T13:46:21  Container 'api' was terminated with exit code '137'
```

`exit 137` is SIGKILL. Frequency over 14 days (`ContainerAppSystemLogs_CL`, only
`api` — `worker` / `beat` / `redis` / `terminal` never once):

| Date | api OOM kills |
| --- | --- |
| 07-29 | 6 |
| 07-30 | 16 |
| 07-31 | **56** |
| 08-01 | 7 |
| 08-02 | 6 |
| 08-03 | 1 |
| 08-05 | 11 (by 14:00) |

Direct sampling showed a clean linear ramp of ~27 MiB/min to the 2 GiB limit,
then a restart, then the same ramp again. This had already been "fixed" once by
raising the limit (`0f406bb5 chore(infra): bump api sidecar to 1.0 vCPU / 2.0Gi`
in June, from 1 Gi) — treating the symptom, so it simply moved the wall.

### Root cause, measured not guessed

The repo's own opt-in sampler (`API_MEMTRACE_INTERVAL_SECONDS`,
`API_MEMTRACE_TRACEMALLOC`) was enabled on the live api sidecar. `tracemalloc`
put the allocations in large transient HTTP buffers, not in a growing object
graph:

| Live sample | Bytes |
| --- | --- |
| `urllib3/response.py:185` | **139.6 MiB** |
| `xml/etree/ElementTree.py:1335` | 27.3 MiB |
| `azure/storage/blob/_generated/models/_models_py3.py:808` | 14.2 MiB |
| `json/decoder.py:354` | 18.2 MiB |
| `requests/models.py:1043` | 5.2 MiB |

Meanwhile `gc_objects` stayed flat around 280-290 k while RSS climbed past
1.7 GiB — the live object graph was **not** growing. The gap was the allocator:
glibc keeps freed arenas rather than returning them to the OS, so every large
blob-download-then-parse left the RSS high-water mark a little higher, forever.

Enabling `API_MALLOC_TRIM=1` proved it outright — `malloc_trim(0)` reclaimed
**40-47 % of RSS on every single sample**:

```
14:17:58  before= 652MiB  after= 383MiB  reclaimed= 269MiB (41.3%)
14:19:01  before= 600MiB  after= 317MiB  reclaimed= 283MiB (47.1%)
14:21:02  before= 547MiB  after= 326MiB  reclaimed= 221MiB (40.4%)
14:22:01  before= 562MiB  after= 333MiB  reclaimed= 229MiB (40.7%)
```

The true live heap is ~320-380 MiB. Everything above that was reclaimable.

## User-facing change

The api sidecar stops being SIGKILL'd, so the dashboard stops dropping requests
(`503`) and losing its in-process caches every ~45 minutes.

## Change summary

* [api/app/memory_diagnostics.py](../../../api/app/memory_diagnostics.py): new
  `start_arena_reclaimer()` — a **default-ON** daemon thread that calls
  `malloc_trim(0)` every 60 s. It is deliberately separate from the diagnostics
  sampler: no `tracemalloc`, no per-minute log line, no `gc.get_objects()` walk,
  so it carries none of the sampler's cost. It probes `malloc_trim` once up
  front and does not start at all on a libc without it (musl), rather than
  spinning forever doing nothing. Kill switch / tuning:
  `API_ARENA_RECLAIM_INTERVAL_SECONDS` (`0` disables; values are floored at 10 s
  so a fat-fingered override cannot become a busy loop).
* [api/app/lifespan.py](../../../api/app/lifespan.py): start it alongside the
  existing sampler and stop it on shutdown.
* Module context header rewritten — the file now owns two pieces with opposite
  defaults (ON mitigation, OFF diagnostics), and that distinction is the thing a
  future reader most needs.

This does **not** reduce the peak allocation. Streaming the large result
downloads instead of materialising them is the real follow-up; the reclaimer
makes the current behaviour survivable in the meantime, which is the difference
between "OOM every 45 min" and "flat".

## Validation

* `uv run pytest -q api/tests/test_memory_diagnostics.py` — 12 passed, including
  four new cases: default-ON, kill switch, no-start when `malloc_trim` is
  unavailable, and the interval floor.
* `uv run pytest -q api/tests` — 4967 passed, 3 skipped.
* `uv run ruff check api` — clean.
* Live: with the trim active the api RSS held at 545-652 MiB (trimmed to
  320-380 MiB) across every sample instead of ramping to 2 GiB.

## Operational note

The temporary diagnostic env vars (`API_MEMTRACE_INTERVAL_SECONDS`,
`API_MEMTRACE_TRACEMALLOC`, `API_MEMTRACE_TOPN`, `API_MALLOC_TRIM`) were set on
the live `api` container to find this and are removed once the code fix is
deployed — `tracemalloc` is not something to leave running in production.
