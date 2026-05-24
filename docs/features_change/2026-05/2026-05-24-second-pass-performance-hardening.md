# Second-pass performance hardening

## Motivation

A second read-only critique found remaining latency and deploy-loop costs after
the first performance batch. The highest-impact residuals were repeated
subscription enumeration on `/api/me`, large Storage listing defaults, hidden-tab
frontend timers, oversized initial Vite chunks, mutable terminal toolchain refs,
and avoidable quick-deploy provider-registration checks.

## User-facing change

Dashboard startup, large-result navigation, and background-tab behaviour should
be lighter. Fast deploy iterations should also avoid unnecessary provider checks
and rebuild drift. No API response schema changed.

## Code-level diff summary

1. `/api/me` now caches visible subscription discovery for 60 seconds
   (`ME_SUBSCRIPTIONS_TTL_SECONDS`) and caps the list at 100 subscriptions
   (`ME_SUBSCRIPTIONS_LIST_LIMIT`).
2. Storage monitor usage defaults now cap per-container scans at 10,000 blobs,
   and result blob listing caps at 5,000 rows by default
   (`STORAGE_RESULT_BLOB_LIST_LIMIT`).
3. Request detail inspector body capture is opt-in by default via
   `REQUEST_DETAIL_CAPTURE_ENABLED=true`, avoiding mutation-body buffering in
   normal runs.
4. The frontend [Vite](https://vite.dev/) build now splits React, TanStack
   Query, xterm, BLAST submit/results, and terminal chunks explicitly.
5. The high-frequency BLAST jobs/history/log timers touched in this batch pause
   in hidden tabs, and BLAST job rows/details are memoized where the render path
   was repeatedly rebuilding styles or rows. Other long-running operation pollers
   keep their existing lifecycle semantics.
6. Terminal toolchain builds pin kubectl, ttyd, and the sibling
   `elastic-blast-azure` ref instead of fetching floating latest/master refs.
7. Quick deploy provider registration has a one-hour marker cache and an
   explicit `SKIP_PROVIDER_REGISTRATION=true` fast path.
8. Docker build contexts exclude generated web/api/test artifacts, and the
   frontend [Docker](https://docs.docker.com/) build pins the Node base image.

## API / IaC diff

No route schema changes and no Bicep changes in this second-pass batch. Docker
and deploy-script behaviour changed for deterministic/cached builds.

## Validation evidence

* `uv run pytest -q api/tests/test_me_route.py api/tests/test_storage_data.py api/tests/test_request_metrics_detail.py` — **45 passed**.
* `uv run pytest -q api/tests` — **1373 passed**.
* `uv run ruff check` on touched Python files — **clean**.
* `cd web && npm run build` — **passed**. The manual chunk split reduced the
   former single large app bundle into route/vendor chunks; two lazy route chunks
   still exceed Vite's 500 kB warning threshold and remain candidates for deeper
   route-level lazy loading.
* `bash -n scripts/dev/quick-deploy.sh scripts/dev/terminal-base-image.sh scripts/dev/acr-build-access.sh` — **passed**.
* `curl -fsI https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64` — **passed**.
* `git fetch --depth 1 origin f4b8b734a82285a18a2ca9aadcbe02759d13f903` against
   `dotnetpower/elastic-blast-azure` — **passed**.
* `az bicep build --file infra/main.bicep --stdout /tmp/elb-main-bicep-build.json` — **passed** with only the Azure CLI Bicep version-upgrade warning.
* `git diff --check` — **clean**.
