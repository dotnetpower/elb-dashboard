# 2026-05-15 — Sidecars event-driven animation: critical hardening pass

## Motivation

Post-implementation review of the new event-driven Sidecars animation
([2026-05-15-sidecars-event-driven-animation.md](./2026-05-15-sidecars-event-driven-animation.md))
turned up five real defects that would surface under sustained load,
adversarial network conditions, accessibility settings, or normal
multi-tab usage. None were show-stoppers in light dev use, but each
would cause a memory or latency regression in a longer-running session.

| # | Severity | Defect |
|---|---|---|
| L1 | leak | Users with `prefers-reduced-motion: reduce` get CSS `animation: none`, so `onAnimationEnd` never fires. Spawned particles accumulate in React state forever. |
| L2 | leak | Same root cause for hidden / collapsed / off-screen cards (`display: none`, background tab, dashboard collapsed): no animation event → no cleanup. |
| H1 | bottleneck | `emit()` used a 0.5 s socket timeout and only disabled the client on `from_url` failure. Each in-flight request paid up to 500 ms when Redis was flaky, and `drain()` had no parallel circuit-breaker. |
| H2 | race | Both the SSE stream and the `/sidecars` polling-fallback drained the same Redis hash atomically. With multiple concurrent readers (multi-tab, polling-on-error, monitoring probes) events scatter across consumers and badges silently drop. |
| M1 | re-render | `setLastCounts(rawCounts)` always created a new object reference, forcing a re-render every snapshot tick even when counts hadn't changed. |

## Fixes (backend)

* **Circuit breaker tuning** (`api/services/event_emitter.py`):
  * Defaults dropped to `EVENT_EMIT_CONNECT_TIMEOUT_SECONDS=0.05` /
    `EVENT_EMIT_SOCKET_TIMEOUT_SECONDS=0.05` (override via env).
    Loopback Redis is sub-millisecond; anything above 50 ms is already
    a failure we'd rather record than wait for.
  * `_FAILURE_COOLDOWN_SECONDS=5.0` keyed on `time.monotonic()`. Trips
    on any `RedisError` from `_get_client`, `emit`, or `drain` and
    suppresses further attempts until the window elapses.
  * `_int_env` / `_float_env` helpers swallow malformed env values and
    fall back to safe defaults — prevents a typo'd env var from
    crashing import.
  * `_normalise_count(count)` clamps at `EVENT_EMIT_MAX_COUNT=1000` so
    a misbehaving caller can't pin Redis writing huge integers.
  * **`drain()` now also arms the breaker on Redis error** — same code
    path as `emit()`. Stops the next 5 s SSE tick from re-paying the
    full timeout while Redis is misbehaving.
* **No-drain snapshot mode** (`api/services/sidecar_metrics.py`,
  `api/routes/monitor.py`):
  * `collect_snapshot(..., drain_events: bool = True)`. When `False`,
    the function returns an all-zero events dict without ever touching
    the Redis hash.
  * `GET /api/monitor/sidecars` (the SPA's initial mount + polling
    fallback) now calls with `drain_events=False`. The SSE stream
    `/api/monitor/sidecars/events` keeps draining — it is the
    *canonical* drainer. This eliminates the poll-vs-SSE half of H2:
    only the live SSE consumer pulls events out of Redis.
* **In-process SSE fan-out broadcaster** (`api/routes/monitor.py`,
  `_SidecarBroadcaster`):
  * One background `asyncio.Task` owns the Redis drain. Every 5 s it
    calls `collect_snapshot(drain_events=True)` *once* and pushes the
    pre-serialised SSE frame into every subscriber's bounded
    `asyncio.Queue`. Two browser tabs (or any number of EventSource
    consumers) now see *the same* event counts every tick — neither
    can steal from the other.
  * Lifecycle: first `subscribe()` spawns the drain task; last
    `unsubscribe()` cancels it (no Redis traffic when nobody's
    watching). FastAPI lifespan calls `close()` to wake any in-flight
    consumers with a sentinel before the process exits.
  * Slow-subscriber policy: per-queue cap is 8 frames; on overflow
    the oldest frame is dropped so the freshest snapshot still gets
    through. Currentness > completeness for a monitoring UI.
  * Initial snapshot for new subscribers is captured with
    `drain_events=False` under the broadcaster lock, so a connecting
    tab never steals a tick from the running drain.

## Fixes (frontend)

* **Reduced-motion + visibility-aware spawning**
  (`web/src/components/cards/SidecarsCard.tsx`):
  * New `useReducedMotion()` and `usePageVisible()` hooks.
  * `useEventParticles` early-returns *before spawning DOM particles*
    when either flag is set. The `lastCounts` badge still updates so
    the per-row counter stays accurate.
* **Hard timeout cleanup** (the L1/L2 fix proper):
  * Each spawned particle gets a `setTimeout(remove, PARTICLE_LIFETIME_MS + delay*1000)`.
    `onAnimationEnd` is still wired up; the timer is the belt to the
    animation suspenders. Idempotent `remove` makes both paths safe.
  * `timersRef` is a `Map<number, Handle>`; an unmount-cleanup effect
    clears all pending timers.
* **Queue hard cap**: `PARTICLE_QUEUE_HARD_CAP = 64`. New particles
  drop the oldest when over the cap (FIFO). Even pathological bursts
  (Redis hash returning 1000 events for one row) can't grow the React
  state past 64 nodes per row.
* **Stable counts**: `setLastCounts` only fires when at least one
  field actually changed, killing the no-op re-render every 5 s.

## Tests

* `api/tests/test_event_emitter.py` (now 13 tests, all pass):
  * `test_invalid_tuning_env_values_fall_back`
  * `test_emit_clamps_large_counts`
  * `test_drain_clamps_bad_counter_values`
  * `test_emit_opens_cooldown_after_redis_error`
  * `test_drain_failure_opens_cooldown` *(new — drain breaker symmetry)*
  * `test_collect_snapshot_skips_drain_when_disabled` *(new — H2 race fix)*
* `api/tests/test_sidecar_broadcaster.py` (new — 4 tests, all pass):
  * `test_two_subscribers_see_identical_frames` — proves H2 fan-out.
  * `test_drain_task_stops_when_last_subscriber_leaves` — no Redis
    traffic when the dashboard is closed.
  * `test_slow_subscriber_drops_oldest_not_block_others` — slow
    consumers can't deadlock the broadcaster.
  * `test_close_wakes_subscribers_with_sentinel` — lifespan shutdown
    is graceful.
* Full backend suite: `uv run pytest -q api/tests` → **120 passed**.
* `npm run build` (in `web/`) → clean.

## Validation evidence

End-to-end against the local 6/6 compose stack (`http://localhost:18080/`):

```
$ docker exec elb-control-local-redis-1 redis-cli -n 2 DEL sidecar:events
$ for i in $(seq 1 20); do
    curl -fsS -X POST http://127.0.0.1:18080/api/health/celery/enqueue-noop >/dev/null &
  done
$ for i in $(seq 1 8); do curl -fsS http://127.0.0.1:18080/api/me >/dev/null & done
$ wait
$ docker exec elb-control-local-redis-1 redis-cli -n 2 HGETALL sidecar:events
row1   28      # 20 enqueue-noop + 8 me — every emit landed
row2   20      # 20 enqueue-noop                — every emit landed
```

Browser DOM right after a 30-request burst against the running SPA:

```
generic [ref=e338]: ws / exec ↣1
```

i.e. the row badge and (during the 1.6 s window) particle render. CPU
on the affected sidecars correspondingly spiked (`api 95% / 56%`,
`worker 13.2%`, `beat 13.6%`).

Multi-subscriber proof (in-process Python harness opening **two** SSE
streams against the running compose stack, then bursting 15 + 7
requests):

```
consumer A non-zero frames: [{'row1': 22, 'row2': 15, 'row3': 0, 'row4': 0}]
consumer B non-zero frames: [{'row1': 22, 'row2': 15, 'row3': 0, 'row4': 0}]
identical? True
```

Both consumers see *the same* counts — the broadcaster is the sole
drainer, no event was stolen from either side. (`22 = 15 enqueue + 7 me`
on row1, `15 = 15 enqueue` on row2.)

## Out of scope

* No new dependencies.
* No legacy/ touched.
* No SAS issuance, no Storage public-access change.
