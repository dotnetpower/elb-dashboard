# Control Plane Sidecars card — cgroup → redis → SSE pipeline

## Motivation

The dashboard previously had no visibility into the six in-revision
sidecars (`frontend`, `api`, `worker`, `beat`, `redis`, `terminal`) of
the `ca-elb-control` Container App. Per the design review at
`/sidecar-design-preview` the user picked the topology proposal and
asked for **near-real-time** CPU/MEM (not the ~1-minute App Insights
lag) and **SSE-pushed** updates. This change ships the full pipeline.

## User-facing change

A new "Control Plane Sidecars" card on the Dashboard (between the 4-up
monitoring grid and BLAST Jobs):

* Topology view of the four data channels — `Browser ↣ frontend → api`,
  `Async ↣ redis → worker`, `Scheduled ↣ beat` (single-node row),
  `ws/exec ↣ api → terminal`.
* Per-sidecar CPU% / MEM% pulled from each container's own cgroup v2
  files every 5 s, published to Redis db 2 by a tiny in-process reporter
  thread/process.
* Animated traffic dot travels left→right along each healthy row;
  degraded edges become amber dashes with no animation (so a blocked
  channel is visually obvious).
* Header pill toggles between `● Near real-time · 5s` (SSE delivering)
  and `● Polling · 30s` (SSE failed → fallback). `Connecting…` while
  acquiring the SSE ticket.

The earlier `/sidecar-design-preview` route + page have been removed —
the live card supersedes them.

## Architecture

```
                ┌────────────┐
                │ frontend   │  cgroup_reporter (python via supervisord)
                │            ├──┐
                ├────────────┤  │     SETEX every 5 s
                │ api        ├──┤     key = sidecar:metrics:<name>
                │ (thread)   │  │     ttl = 30 s
                ├────────────┤  │
                │ worker     ├──┼──► Redis db 2  (loopback :6379)
                │ (thread)   │  │
                ├────────────┤  │
                │ beat       ├──┤
                │ (thread)   │  │
                ├────────────┤  │     INFO  (no reporter)
                │ redis ◄────┼──┘     used_memory + cpu deltas
                ├────────────┤
                │ terminal   │  cgroup_reporter (python subprocess)
                │ (process)  │
                └────────────┘

           api  ─────► /api/monitor/sidecars            GET (snapshot)
                ─────► /api/monitor/sidecars/ticket     POST (one-shot)
                ─────► /api/monitor/sidecars/events     GET  (SSE)

           SPA  ─────► EventSource(events?ticket=…)     5 s push
                ─────► useQuery(snapshot)               30 s polling fallback
```

## API / IaC diff summary

### Backend (`api/`)
* New `api/services/cgroup_reporter.py` — pure-function helpers + a
  daemon-thread loop that publishes `sidecar:metrics:<name>` every 5 s.
* New `api/services/sidecar_metrics.py` — single `MGET` over the reporter
  keys, fills Redis's own slot from `INFO memory` + `INFO cpu` deltas,
  computes `health` from staleness (`>10 s` = degraded, `>15 s` = down),
  and isolates malformed reporter payloads per sidecar instead of failing
  the whole dashboard snapshot.
* Hardened Redis outage behavior — if Redis cannot serve the metrics `MGET`,
  the API now returns a stable all-down degraded snapshot with
  `degraded_reason = "redis_unavailable"` instead of bubbling an exception
  to the route-level empty fallback or SSE error frames.
* `api/main.py` — startup hook spawns the reporter unless
  `SIDECAR_REPORTER_DISABLED=true` (used in unit tests).
* `api/celery_app.py` — `worker_init` / `beat_init` Celery signals fire
  the same reporter for those sidecars.
* `api/routes/monitor.py` — three new endpoints:
  * `GET  /api/monitor/sidecars` — one-shot snapshot.
  * `POST /api/monitor/sidecars/ticket` — single-use opaque token (30 s TTL).
  * `GET  /api/monitor/sidecars/events?ticket=…` — SSE stream
    (`event: snapshot` every 5 s, `: heartbeat` every 25 s).
* New tests:
  * `api/tests/test_cgroup_reporter.py` — 5 cases covering CPU% math.
  * `api/tests/test_sidecar_metrics.py` — 10 cases covering the staleness
    classifier, malformed JSON, non-object payloads, bad timestamps, Redis
    self-info degradation, Redis outage all-down snapshots, and CPU deltas.

### terminal sidecar
* `terminal/Dockerfile` — installs `redis==5.2.0` into `/opt/elb/venv`,
  copies the standalone `cgroup_reporter.py` to
  `/usr/local/bin/elb-cgroup-reporter`.
* `terminal/cgroup_reporter.py` — slim mirror of the api version
  (build context is `terminal/`, can't import `api.*`).
* `terminal/entrypoint.sh` — supervisor loop now runs **three**
  children (ttyd, exec_server, reporter). The reporter is
  intentionally *excluded* from `wait -n` so telemetry loss does not
  cycle the revision.

### frontend sidecar
* `web/Dockerfile` — switched runtime to nginx + python3 + supervisord
  (≈+15 MiB image), runs nginx + reporter together.
* New `web/supervisord.conf`, new `web/cgroup_reporter.py` (mirror).

### Bicep
* `infra/modules/containerAppControl.bicep` — every container that has
  an `env:` block now exports `SIDECAR_NAME` + `OPS_REDIS_URL`. The
  frontend container gained an `env:` block.

### Frontend (`web/src/`)
* New `web/src/hooks/useSidecarMetrics.ts` — ticket → `EventSource` →
  bounded backoff (5/15/45 s) → polling fallback via TanStack Query.
* New `web/src/components/cards/SidecarsCard.tsx` — extracted topology
  proposal #3 from the design preview, wired to the hook, with the
  same particle/keyframe animation.
* `web/src/pages/Dashboard.tsx` — render `<SidecarsCard />` between the
  4-up grid and the JobCard.
* Removed `web/src/pages/SidecarDesignPreview.tsx` and its
  `/sidecar-design-preview` route from `web/src/App.tsx`.

## Validation evidence

```
$ cd /home/moonchoi/dev/elb-dashboard && uv run ruff check api/services/sidecar_metrics.py api/tests/test_sidecar_metrics.py
All checks passed!

$ cd /home/moonchoi/dev/elb-dashboard && uv run pytest -q api/tests/test_sidecar_metrics.py
..........                                                               [100%]
10 passed in 0.06s

$ cd /home/moonchoi/dev/elb-dashboard && uv run pytest -q api/tests
........................................................................ [ 94%]
....                                                                     [100%]
76 passed in 9.69s

$ cd web && npx tsc --noEmit -p .
exit=0

$ curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/api/monitor/sidecars
200

$ curl -s -X POST http://localhost:8080/api/monitor/sidecars/ticket
{"ticket":"…","ttl_seconds":30}

$ TICKET=…; timeout 12 curl -sN "http://localhost:8080/api/monitor/sidecars/events?ticket=$TICKET"
event: snapshot
data: {"degraded":true,"degraded_reason":"redis_unavailable",...}
# (no Redis in local dev — the card still receives a renderable snapshot;
#  with Redis these frames contain reporter metrics plus Redis self-info)
```

Browser smoke (local dev, no Redis sidecar):
* Card renders on the dashboard between the 4-up grid and BLAST Jobs.
* Header shows `● Polling · 30s` until SSE connects and `0/6 healthy`
  when Redis is unavailable; the snapshot remains renderable with
  `degraded_reason = "redis_unavailable"`.
* All six sidecars render as **Down** with no animation — exactly the
  intended "honest" state.

Container Apps smoke (next deploy): `SIDECAR_NAME` env var per
container, all five reporter writers + Redis self-info will populate
the Redis db 2 keys; the SPA card switches to `● Near real-time · 5s`
and traffic dots animate along healthy edges.

## Files touched

```
api/celery_app.py
api/main.py
api/routes/monitor.py
api/services/cgroup_reporter.py            (new)
api/services/sidecar_metrics.py            (new)
api/tests/test_cgroup_reporter.py          (new)
api/tests/test_sidecar_metrics.py          (new)
infra/modules/containerAppControl.bicep
terminal/Dockerfile
terminal/cgroup_reporter.py                (new)
terminal/entrypoint.sh
web/Dockerfile
web/cgroup_reporter.py                     (new)
web/supervisord.conf                       (new)
web/src/App.tsx
web/src/components/cards/SidecarsCard.tsx  (new)
web/src/hooks/useSidecarMetrics.ts         (new)
web/src/pages/Dashboard.tsx
web/src/pages/SidecarDesignPreview.tsx     (deleted)
```

## Future work

* **Multi-replica safety** — the ticket store is process-local. Today
  `minReplicas == maxReplicas == 1` so this is fine; if scale-out is
  ever introduced the ticket store has to move into the same Redis db
  2 (small change).
* **Drop the standalone reporters** if/when `web/Dockerfile` adopts the
  same `uv`-managed venv we use for api — at that point all five
  reporters can `from api.services.cgroup_reporter import …`.
* **SSE auto-resume after network blip** — current behaviour closes the
  EventSource on any `error` event and re-issues a ticket. That works
  but loses one snapshot frame; a future iteration could keep the
  EventSource alive and only re-ticket when the server explicitly
  closes with a 4xx.
