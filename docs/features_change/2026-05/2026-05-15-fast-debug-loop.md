# 2026-05-15 — Fast Container App debug loop

## Motivation

Each Azure Container Apps debug cycle has been costing 5-10 minutes:

```
edit → az acr build (~2-3 min) → az deployment group create (~1-2 min)
     → wait revision (~60 s) → az containerapp logs show (snapshot)
```

Over the last week the team burned multiple of these cycles re-discovering
that the bugs (`celery shared_task` routing trap, redis startup ordering,
terminal `exec_server` contract, route registration order vs. the
`frontend_proxy` catch-all) are **app-logic bugs, not infra bugs** — they
are reproducible without ARM, without ACR, and without a private endpoint.

The existing `scripts/dev/docker-compose.local.yml` only carried two
sidecars (api + frontend), so none of those failure modes could be caught
locally.

## User-facing change

A new three-tier debug loop, documented in
[`scripts/dev/README.md`](../../scripts/dev/README.md):

| Tier | Tool | Cycle time | Catches |
| --- | --- | --- | --- |
| 1 | `uv run pytest -q api/tests` | seconds | Pure unit logic |
| 2 | `docker compose -f scripts/dev/docker-compose.full.yml up --build` | ~5 s incremental | Celery routing, redis-wait, terminal exec, route order, WS proxy, SPA fallback |
| 3 | `scripts/dev/quick-deploy.sh <sidecar> [--logs]` | ~1-2 min | MI / private endpoint / real Storage |

Tier 2 is **new**: a 6-sidecar mirror of the Container App with `api/`
bind-mounted into api/worker/beat for live `uvicorn --reload`, a real
`redis:7-alpine`, the real terminal sidecar (`exec_server` bound to
`0.0.0.0` via `EXEC_HOST` for cross-container reach), and vite dev with
HMR for the SPA. Wired with the same env contract the Bicep uses
(`CELERY_BROKER_URL`, `TERMINAL_EXEC_UPSTREAM`, `EXEC_TOKEN`, etc.) so the
upstream URLs are the only things rebound.

Tier 3 is **new**: a one-image quick deploy that does a single `az acr
build` for the changed sidecar and a single `az containerapp update
--container-name <x> --image <new>` (no Bicep, no env / probe / secret
changes). When the api image changes, `worker` and `beat` are bumped to
the same tag automatically — they share `elb-api`, and leaving them on a
stale tag was the root cause of last week's "fix lands but task still
runs old code" confusion.

## Files

- New: [`scripts/dev/docker-compose.full.yml`](../../scripts/dev/docker-compose.full.yml)
- New: [`scripts/dev/quick-deploy.sh`](../../scripts/dev/quick-deploy.sh)
- New: [`scripts/dev/README.md`](../../scripts/dev/README.md)

No production code changed. The terminal sidecar's `exec_server` already
honours `EXEC_HOST` (default `127.0.0.1`); compose just sets it to
`0.0.0.0` so the api container on the bridge network can reach
`terminal:7682`. ttyd's loopback bind is unchanged in production.

## Validation evidence

```bash
$ bash -n scripts/dev/quick-deploy.sh && echo OK
OK

$ docker compose -f scripts/dev/docker-compose.full.yml config -q && echo OK
OK
```

A live-build smoke is intentionally not run as part of this commit —
`docker compose ... up --build` triggers two image builds (api + terminal)
that take several minutes; the developer running the loop will pay that
cost once, then iterate at near-zero marginal cost via the bind mounts.

## Out of scope

- No change to `postprovision.sh`. Sidecar layout / env / probes / secrets
  still flow through Bicep there.
- No change to the production `EXEC_TOKEN` story — the dev token is a
  fixed string in compose only.
- No CI integration of `docker-compose.full.yml`. (Tier 1 pytest already
  runs in CI.)
