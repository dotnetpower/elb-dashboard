# 2026-05-15 — OpenAPI Update button + local-run terminal-exec sidecar

## Motivation

Two follow-ups to the prior `/docs` deploy work:

1. **The previous deploy actually still failed** because the api / worker
   started by `scripts/dev/local-run.sh` had no `EXEC_TOKEN` env var, so
   `api.services.terminal_exec.run()` raised
   `TerminalExecError: EXEC_TOKEN env var is empty …` the moment the deploy
   task tried to `kubectl apply` the manifests. The `setup_workload_identity`
   phase succeeded (Azure SDK only) but `applying_manifests` died.
2. **The sibling [`elastic-blast-azure`](https://github.com/dotnetpower/elastic-blast-azure)
   repo bumps the `elb-openapi` image tag periodically.** When that
   happens the dashboard's `IMAGE_TAGS["elb-openapi"]` is updated, but
   the running pod on AKS still uses the old image — the user had no UI
   affordance to re-roll the deployment.

## User-facing change

- **New "Update OpenAPI service" card on `/docs`** that appears when the
  service is already running. Shows the pinned tag (e.g. `v3.4`) and an
  **Update to v3.4** button that re-runs the existing
  `deploy_openapi_service` Celery task. Because the deployment manifest
  uses `imagePullPolicy: Always`, recreating the pod pulls the latest
  image for the pinned tag — exactly what's needed after a sibling-repo
  bump. **Retry Discovery** stays alongside.
- The pre-existing "Deploy elb-openapi" card on first install is
  unchanged.
- Local-dev only: `scripts/dev/local-run.sh` gains a `terminal-exec`
  service that runs `terminal/exec_server.py` on `127.0.0.1:7682`, and
  `local-run.sh api` / `local-run.sh worker` now auto-set
  `EXEC_TOKEN` + `TERMINAL_EXEC_UPSTREAM` so the deploy task works
  without docker-compose. Production (Container Apps) is unaffected —
  the secret is still injected by Bicep there.

## API / IaC diff summary

### Frontend
- `web/src/components/OpenApiDeployPanel.tsx`
  - New props: `variant?: "deploy" | "update"`, `pinnedTag?`, `currentTag?`.
  - In `update` mode: muted border, `RotateCw` icon, "Update OpenAPI
    service" header with pinned-tag badge, copy referencing the sibling
    repo, button label "Update to v<tag>" / "Updating…". All existing
    state machine (stale-PENDING guard, Cancel button, etc.) reused.
  - Imported `RotateCw` from `lucide-react`.
- `web/src/pages/ApiReference.tsx`
  - Renders `<OpenApiDeployPanel variant="update" … />` whenever
    `baseUrl && hasOpenApiImage`. `pinnedTag` reads from
    `acrQuery.data.expected_image_tags["elb-openapi"]`, `currentTag`
    from `actual_tags["elb-openapi"]?.[0]`.
  - Update's `onRetry` re-fetches both `svcQuery` and `specQuery` so the
    swagger view re-renders against the new pod.

### Local dev tooling
- `scripts/dev/local-run.sh`
  - New `with_terminal_exec_env` helper sets a non-secret dev token
    (`EXEC_TOKEN=dev-exec-token-not-secret-but-long-enough-for-startup-check`)
    and `TERMINAL_EXEC_UPSTREAM=http://127.0.0.1:7682` if the caller
    didn't override them. Wired into the `api` and `worker` cases.
  - New `terminal-exec` case runs `python3 terminal/exec_server.py`
    after asserting `az`, `kubectl`, `azcopy` are on PATH. The server
    binds 127.0.0.1 only, identical security model to the Container App
    deployment.
  - Usage updated.

### Backend
- No backend code changes (the deploy Celery task is reused as-is).

## Validation evidence

- Started `local-run.sh terminal-exec`; `curl 127.0.0.1:7682/healthz`
  → `{"status":"ok","max_concurrency":4}`.
- Restarted `local-run.sh api` and `local-run.sh worker`; verified
  `cat /proc/<worker-pid>/environ` contained
  `EXEC_TOKEN=<set, length=60>` and
  `TERMINAL_EXEC_UPSTREAM=http://127.0.0.1:7682`.
- `curl POST /api/aks/openapi/deploy …` → progressed
  `Pending → setup_workload_identity → applying_manifests →
  waiting_for_external_ip → completed` in 36 seconds. Final output:

  ```json
  {
    "status": "succeeded",
    "openapi_deploy": {
      "status": "deployed",
      "image": "elbacr01.azurecr.io/elb-openapi:3.4",
      "external_ip": "20.249.147.217",
      "apply_output": "serviceaccount/elb-openapi-sa created\n…"
    },
    "elapsed_seconds": 36
  }
  ```
- `curl /api/monitor/aks/service-ip?…service_name=elb-openapi` →
  `{"service_name":"elb-openapi","external_ip":"20.249.147.217"}`.
- Browser screenshot of `/docs` shows the new **Update OpenAPI service**
  card with **v3.4** badge, **Update to v3.4** button, and **Retry
  Discovery** button rendering correctly alongside the live
  `http://20.249.147.217` Swagger UI link in the hero.
- `npx tsc --noEmit` in `web/` — no errors.
- `uv run pytest -q api/tests` — **123 passed**.

## Operational note

`local-run.sh terminal-exec` is the new canonical local-dev pattern for
end-to-end deploy testing. Previous guidance to use `compose-full` for
this purpose still works but is no longer required for openapi-deploy
specifically.

If you `kill_terminal` a worker terminal during local dev, **also kill
the orphaned `uv run celery …` child** (e.g. `pkill -f "celery.*worker"`)
before starting a new one — otherwise the orphan keeps consuming tasks
on the broker without the new env vars and silently fails them.
