# Live Wall log tail — Container Apps fallback via Log Analytics

## Motivation

The Live Wall sidecar tiles ([web/src/pages/Monitor/LiveWall.tsx](../../../web/src/pages/Monitor/LiveWall.tsx)) consumed `GET /api/monitor/logs/{container}/recent` and `events` (SSE), both backed by [api/services/sidecar_logs.py](../../../api/services/sidecar_logs.py) reading **local files at `<project>/.logs/local/latest/<container>.log`**. Those paths only exist when `scripts/dev/local-run.sh` writes them — in the deployed Container App every sidecar logs straight to `stdout`/`stderr`, no volume is mounted, and the api sidecar cannot read another sidecar's stream. Result: in production every Live Wall tile rendered "live" with **0 log lines forever** — including AKS provision and BLAST submit activity. This was diagnosed by querying `ContainerAppConsoleLogs_CL` directly, which confirmed Container Apps was capturing every request and Celery line; only the in-app surface was blind.

## User-facing change

In the deployed Container App the Live Wall log tiles now stream the real sidecar stdout/stderr by querying the Log Analytics workspace that the Container Apps Environment is already wired to. Local-dev behaviour is unchanged — the file tail path remains the default whenever the `CONTAINER_APP_NAME` env marker is absent.

* Each tile shows up to the last `_MAX_LINES_PER_CONTAINER` (500) lines for that sidecar.
* One LA query per ~5 s serves all six tiles and every browser tab on a process (shared snapshot cache).
* Lookback window is 10 minutes by default; override via `LIVE_WALL_LA_LOOKBACK_MINUTES`.
* Cache TTL override: `LIVE_WALL_LA_CACHE_TTL_SECONDS`.
* Force-disable (back to the file path, useful when the workspace is being rebuilt): `LIVE_WALL_LA_DISABLE=true`.
* Secret masking pipeline (`bearer`, `Authorization:`, SAS query params, etc.) is the same one already applied to the file path — both surfaces share `_render_log_line` now.

## API / IaC diff summary

### Backend

| File | Change |
|---|---|
| [pyproject.toml](../../../pyproject.toml) | New dep `azure-monitor-query==1.4.1`. |
| [api/services/sidecar_logs.py](../../../api/services/sidecar_logs.py) | Extracted `_render_log_line(text, ts_iso)` so the file path and the LA path share the masking + level inference. `read_recent_lines` / `read_lines_since` / `end_offset` now check `_use_la_fallback()` and dispatch to `sidecar_logs_la` when the api runs as a Container Apps sidecar **and** `LOG_ANALYTICS_WORKSPACE_ID` is non-empty. |
| [api/services/sidecar_logs_la.py](../../../api/services/sidecar_logs_la.py) | New module. `LogsQueryClient` constructed lazily via `get_credential()`. One process-wide snapshot keyed by container, refreshed at most every `_CACHE_TTL_SEC` seconds. Snapshot is built from a single KQL query (`ContainerAppConsoleLogs_CL | where ContainerName_s in (...) | project TimeGenerated, ContainerName_s, Log_s | order by TimeGenerated asc`) bound to a 10-minute timespan; the per-container cap is applied in Python after grouping. `read_lines_since_la` keeps the caller's watermark when no new rows are found so the SSE loop does not regress. On query failure the previous snapshot is returned and a warning is logged (then demoted to DEBUG after 3 failures to prevent log spam). |
| [api/tests/test_sidecar_logs_la.py](../../../api/tests/test_sidecar_logs_la.py) | 7 new tests: env-flag dispatch, parse + sanitize, `read_lines_since` watermark contract, snapshot-shared-across-containers, failure-keeps-previous, `read_recent_lines` end-to-end dispatch, `end_offset` empty-snapshot fallback. |

### Infra

| File | Change |
|---|---|
| [infra/modules/monitoring.bicep](../../../infra/modules/monitoring.bicep) | New `uamiPrincipalId` param (defaults to empty to keep tests/what-if happy). When non-empty, the workspace receives a `Log Analytics Reader` (`73c42c96-874c-492b-b04d-ab87d138a893`) role assignment for that principal. Required because **RG-level Contributor does NOT cover the `…/workspaces/query/read` data-plane action** — verified by listing the MI's current role assignments at workspace scope (zero) before this change. |
| [infra/modules/containerAppControl.bicep](../../../infra/modules/containerAppControl.bicep) | New `logAnalyticsWorkspaceId` param. Injected as `LOG_ANALYTICS_WORKSPACE_ID` env var into the `api` sidecar (the only one that runs Live Wall code; worker/beat don't need it). |
| [infra/main.bicep](../../../infra/main.bicep) | Threads `identity.outputs.identityPrincipalId` into `monitoring.bicep` and `monitoring.outputs.workspaceCustomerId` into `containerAppControl.bicep`. |

## Out of scope

* WebSocket `/api/terminal/ws` does not flow through `BaseHTTPMiddleware`, so the `req` completion log line + `row4` emit do not fire on the WebSocket lifetime itself. This is standard Starlette behaviour and matches the existing design (the `/api/terminal/ticket` POST that precedes the upgrade IS captured). Not changed here.
* No new dashboard control / button. The LA fallback is purely transparent — the route surface, SSE event shape, and React hook are all unchanged.

## Validation evidence

* `uv run pytest -q api/tests/test_sidecar_logs.py api/tests/test_sidecar_logs_la.py` → **13 passed** (file path + new LA path).
* `uv run pytest -q api/tests` → **1605 passed** (full backend regression after the dispatch change).
* `uv run ruff check api/services/sidecar_logs.py api/services/sidecar_logs_la.py` → clean.
* `az bicep build -f infra/main.bicep` → compiles, no warnings on the new params.
* Pre-change probe (root-cause evidence): `az monitor log-analytics query --workspace 1a557a86-2dde-47b5-8195-7e04cc3c3640 --analytics-query "ContainerAppConsoleLogs_CL | where ContainerName_s == 'api' | top 5 by TimeGenerated desc"` returned real api sidecar lines from `req rid=…`, confirming the workspace had the data the SPA was blind to.
* Post-deploy verification (to run): redeploy via `azd provision` (Bicep) + `scripts/dev/postprovision.sh` (image rebuild) then open Monitor → Live Wall in the SPA — every tile should populate within ~10 s.
