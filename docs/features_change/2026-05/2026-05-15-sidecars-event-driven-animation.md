# 2026-05-15 — Sidecars topology animation is now event-driven

## Motivation

The "Control Plane Sidecars" dashboard card rendered four particle dots
that animated **infinitely** on a CSS keyframe loop, gated only by each
endpoint being `health === "ok"`. The card's own legend said
"animated dot = live traffic", but the dots had no relationship to
real traffic — they fired forever even when the system was idle.

This violated the dashboard's contract: every number on the page should
come from a real Azure / Kubernetes / sidecar reading. A purely
decorative animation that lies about activity is worse than no animation.

## User-facing change

Each row of the topology now spawns particles **per real event** drained
from the previous 5-second snapshot tick:

| Row | Event source | Counter incremented when |
|-----|--------------|--------------------------|
| `row1` Browser → frontend → api | `RequestIdMiddleware` in `api/main.py` | every non-`/api/health`, non-`/api/monitor/sidecars*` HTTP request lands on the api sidecar |
| `row2` api → redis → worker | Celery `before_task_publish` signal in `api/celery_app.py` (when `SIDECAR_NAME != "beat"`) | api enqueues a Celery task |
| `row3` beat → redis | same signal, but only when `SIDECAR_NAME == "beat"` | beat publishes a periodic task |
| `row4` api ↔ terminal | `RequestIdMiddleware` for any path starting with `/api/terminal/` | terminal WebSocket / exec proxy used |

A small numeric badge next to each row label shows the **exact** count
for the most recent tick (capped at `6+` so a sudden burst doesn't render
a wall of dots — the badge reads `6+` to make the cap visible). When the
count is zero the badge is hidden, so an idle system shows zero motion
and zero badges. The legend now reads `● dot = real event since last tick`.

## API / IaC diff summary

### Backend (`api/`)

- **New** [`api/services/event_emitter.py`](../../../api/services/event_emitter.py):
  Cross-process counter at the natural shared point — Redis hash
  `sidecar:events`. `emit(row, count=1)` does a best-effort `HINCRBY`
  with a cached `redis.Redis` client (`OPS_REDIS_URL`) and **never
  raises** — decorative telemetry must never affect a request. The
  request-path timeout defaults are intentionally tiny
  (`EVENT_EMIT_CONNECT_TIMEOUT_SECONDS=0.05`,
  `EVENT_EMIT_SOCKET_TIMEOUT_SECONDS=0.05`) and a Redis failure opens a
  short circuit breaker (`EVENT_EMIT_FAILURE_COOLDOWN_SECONDS=5`) so a
  degraded broker cannot add repeated half-second stalls to API traffic.
  Counts are clamped by `EVENT_EMIT_MAX_COUNT=1000` to keep a corrupt or
  bursty counter from inflating the SSE payload. `drain(client)` does a pipelined
  `HGETALL` + `DELETE` so each snapshot tick atomically reads-and-resets
  the four row counters.
- [`api/services/sidecar_metrics.py`](../../../api/services/sidecar_metrics.py)
  `collect_snapshot` now drains the counters and includes
  `events: { row1, row2, row3, row4 }` in the payload (the
  `_all_down_snapshot` fallback also returns zeros so the SPA can rely
  on a stable shape).
- [`api/main.py`](../../../api/main.py) `RequestIdMiddleware.dispatch`
  emits `ROW_TERM` for `/api/terminal/*` paths, `ROW_HTTP` otherwise,
  excluding `/api/health` and the sidecar monitor endpoints (those
  fire from the polling/SSE itself and would self-pollute).
- [`api/celery_app.py`](../../../api/celery_app.py) registers a
  `before_task_publish` signal handler that emits `ROW_SCHED` from the
  `beat` sidecar (env `SIDECAR_NAME=beat` set in
  [`scripts/dev/docker-compose.full.yml`](../../../scripts/dev/docker-compose.full.yml))
  and `ROW_ASYNC` from everywhere else.

### Frontend (`web/`)

- [`web/src/hooks/useSidecarMetrics.ts`](../../../web/src/hooks/useSidecarMetrics.ts)
  `SidecarsSnapshot` gets the optional `events?: { row1?: number; … }`
  field with a docstring naming each row.
- [`web/src/components/cards/SidecarsCard.tsx`](../../../web/src/components/cards/SidecarsCard.tsx):
  - `RowParticle` accepts `onEnd`, forces
    `animationIterationCount: "1"` and `animationFillMode: "forwards"`,
    and the `.topo-row-particle` CSS class no longer specifies
    `infinite`.
  - New `useEventParticles(data)` hook keeps a `ParticleEvent[]` queue,
    de-dupes on `snapshot.ts` (so a re-render or fallback poll reading
    the same snapshot doesn't double-fire), spawns up to
    `PARTICLES_PER_TICK_CAP = 6` per row per tick with a 0.18 s stagger,
    and exposes `lastCounts` for the row badge.
  - Each row drops the old health-gated single particle and renders
    `particles.filter(p => p.row === N).map(...)`. Row 3 keeps
    `durationSec={0.9}` and the special
    `endRight="calc((100% - 458px) / 2 + 250px)"` so the dot stops at
    the broker label.
  - Legend updated to "● dot = real event since last tick".

### Infra

None. The Redis sidecar already exists with `OPS_REDIS_URL` shared by
all sidecars (db 2 in
[`scripts/dev/docker-compose.full.yml`](../../../scripts/dev/docker-compose.full.yml)).

## Validation evidence

- `uv run pytest -q api/tests` — **110 passed**, including the new
  [`api/tests/test_event_emitter.py`](../../../api/tests/test_event_emitter.py)
  covering `emit` increment, swallowed unknown rows / non-positive
  counts, `drain` empty / non-empty / unknown-fields / Redis-error
  paths, plus a `collect_snapshot` integration that proves the snapshot
  drains the hash atomically.
- `uv run pytest -q api/tests/test_event_emitter.py` — **11 passed**
  after hardening, covering count clamping, Redis-error cooldown, and
  tolerant fallback when optional `EVENT_EMIT_*` tuning env vars are
  malformed.
- `cd web && npm run build` — clean (`tsc -b && vite build` ✓).
- Compose `docker compose -f scripts/dev/docker-compose.full.yml up -d --no-deps --force-recreate api worker beat frontend` — all 6/6 healthy.
- Backend smoke (no SSE drain in between):
  ```text
  $ for i in 1..3: curl -X POST /api/health/celery/enqueue-noop
  $ curl /api/monitor/sidecars  → events: {row1:3, row2:3, row3:0, row4:0}
  ```
- Browser screenshot of dashboard after a 5×enqueue + 4×/api/me burst:
  Browser badge shows `6+`, Async badge shows `5`, Scheduled / ws-exec
  show no badge (no activity → no dots). With the previous infinite
  CSS animation those last two rows would have been firing dots
  regardless.
