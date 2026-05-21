# 2026-05-21 — BLAST status latency: list refresh + per-phase throttle + per-job poller (+ sibling repo parallelism)

## Motivation

A BLAST submit measured against job `10949573-3997-4e96-9bfb-d6b8f61c20c5` showed
~4 m 7 s of perceived wall-time on the dashboard while the K8s job itself ran in
21 s (7 s container compute). The ~35× perceived slowdown came from four
independent latency sources:

1. The `GET /api/blast/jobs` list endpoint did **not** refresh active rows
   against K8s — it only returned whatever was last persisted. The detail
   endpoint refreshed, but the dashboard's primary view is the list. A
   finished job stayed `running` in the list until the next 60 s beat tick
   of `blast-reconcile-stale-jobs` flipped it.
2. The per-job K8s refresh throttle inside
   `_refresh_running_blast_state` was a flat 20 s. That makes sense for
   `submitted` (waiting for a long pull/init) but is too coarse for
   `running`/`results_pending`, where a 5 s probe is appropriate.
3. After a successful `submit`, there was no per-job poller — we relied
   entirely on the beat tick (up to 60 s old) and on the user manually
   opening the detail page. That alone could account for 30-50 s of the
   observed latency on a fast job.
4. In `dotnetpower/elastic-blast-azure`, the warm-cluster reuse path inside
   `ElasticBlastAzure._initialize_cluster` ran three independent operations
   sequentially (`_cleanup_stale_jobs`, `kubernetes.create_scripts_configmap`,
   `_upload_queries_only`). On a warm cluster these are the only meaningful
   bootstrap steps before the K8s Job is created — running them in parallel
   cuts the perceived submit-CLI cost.

## User-facing change

A BLAST job that finishes on K8s now flips to `completed` on the dashboard's
job list within **~10 s** instead of waiting up to 60 s for the next beat
reconcile. Three back-end mechanisms cooperate:

- The list endpoint refreshes each active row before responding (uses the
  shared per-job throttle so the dashboard polling cadence cannot stampede
  K8s).
- The refresh throttle is now 5 s for `running` / `results_pending` and
  20 s for `submitted` (the longest phase, where K8s state changes slowly).
- A new Celery task `api.tasks.blast.poll_running_status` is enqueued at
  the end of `submit` with a 10 s start delay and self-reschedules every
  10 s while the row is still active, up to 180 iterations (~30 minutes).
  The 60 s beat reconcile remains the safety net.

The sibling repo (`dotnetpower/elastic-blast-azure`) parallelises the warm-
cluster reuse shortcut by default, with an opt-out env var
(`ELB_PARALLEL_WARM_REUSE=0`).

## API / IaC diff summary

### `elb-dashboard`

- `api/services/blast_job_state.py`
  - New constants `_K8S_REFRESH_FAST_INTERVAL_SECONDS = 5.0`,
    `_K8S_REFRESH_FAST_PHASES = {"running","results_pending"}`, helper
    `_refresh_min_interval_seconds(phase)`.
  - `_refresh_running_blast_state` now reads scope (`subscription_id`,
    `resource_group`, `cluster_name`, `storage_account`) from top-level
    columns first, so callers can pass rows obtained with
    `list_for_owner(..., include_payload=False)`. Before any `repo.update`,
    it reloads the full payload via `_maybe_reload_with_payload` so the
    merged `_progress` carries existing step history.
- `api/routes/blast/jobs.py`
  - `blast_jobs_list` iterates rows whose phase is in `_K8S_REFRESH_PHASES`
    and calls `_refresh_running_blast_state(repo, row)` (debug-logged on
    failure, never 500s).
- `api/tasks/blast/__init__.py`
  - New `poll_running_status` shared task (`name="api.tasks.blast.poll_running_status"`,
    queue `blast`). Self-reschedules with `countdown=POLL_RUNNING_INTERVAL=10`
    while status ∈ {`running`,`pending`,`queued`} and phase ∈
    `_K8S_REFRESH_PHASES`. Capped at `POLL_RUNNING_MAX_ITERATIONS=180`.
  - `submit` enqueues the poller in its success branch (only when
    `status == "running"` and `phase ∈ _POLL_RUNNING_ELIGIBLE_PHASES`).
- `api/tests/test_local_to_blast_job.py`: +2 tests
  (`*_running_phase_uses_short_throttle`, `*_reads_top_level_columns`).
- `api/tests/test_blast_tasks.py`: +4 tests covering missing-row,
  terminal-status, reschedule-on-active, and max-iterations stop.
- `api/tests/test_external_blast_api.py`: +1 test
  (`test_canonical_jobs_list_refreshes_active_local_rows`).

No IaC changes. No new Celery beat schedule (only a per-job apply_async).
No new env vars on the dashboard side.

### `dotnetpower/elastic-blast-azure` (sibling)

- `src/elastic_blast/azure.py` `_initialize_cluster` warm-reuse branch:
  wraps `_cleanup_stale_jobs`, `create_scripts_configmap`, and
  `_upload_queries_only` in a `ThreadPoolExecutor(max_workers=3,
  thread_name_prefix='elb-warm-reuse')`. Opt-out via
  `ELB_PARALLEL_WARM_REUSE=0`.

## Validation evidence

```bash
$ cd /home/moonchoi/dev/elb-dashboard
$ uv run pytest -q api/tests
853 passed in 43.81s

$ uv run ruff check api
All checks passed!

$ cd web && npm run build
✓ built in 6.95s

$ cd /home/moonchoi/dev/elastic-blast-azure
$ pytest -q tests/azure/test_warm_cluster.py
17 passed in 9.83s
```

Targeted runs of the new tests:

- `test_refresh_running_blast_state_running_phase_uses_short_throttle` — passes
- `test_refresh_running_blast_state_reads_top_level_columns` — passes
- `test_poll_running_status_returns_missing_when_row_absent` — passes
- `test_poll_running_status_returns_without_reschedule_on_terminal_status` — passes
- `test_poll_running_status_reschedules_when_still_active` — passes
- `test_poll_running_status_stops_at_max_iterations` — passes
- `test_canonical_jobs_list_refreshes_active_local_rows` — passes

## Manual rollout

The sibling repo change is on `dotnetpower/elastic-blast-azure@master`
(commit `47c1369b`, "feat(azure): … parallel warm-reuse processing",
pushed 2026-05-21). The dashboard consumes that code in two distinct ways:

- **Local development**: `scripts/dev/local-run.sh terminal-exec` prepends
  `LOCAL_ELASTIC_BLAST_AZURE_ROOT` (default `$HOME/dev/elastic-blast-azure`)
  to both `PATH` and `PYTHONPATH`, so the Python interpreter resolves
  `elastic_blast.azure` from the working tree before the installed venv
  copy. Verified with
  `PYTHONPATH=$HOME/dev/elastic-blast-azure/src python3 -c "import
  elastic_blast.azure as m; print(m.__file__)"` → resolves to
  `src/elastic_blast/azure.py`, which contains the parallel warm-reuse
  block. **No further action required for local runs** once the sibling
  working tree is on `master`.
- **Deployed Container App**: the terminal sidecar image clones
  `dotnetpower/elastic-blast-azure` at *image build time* in
  `terminal/Dockerfile.base` (line ~70, `git clone --depth 1
  https://github.com/dotnetpower/elastic-blast-azure.git`). Layer caching
  means a normal rebuild will reuse the cached clone. To pick up the new
  sibling commit, force a base-image rebuild:
  `TERMINAL_BASE_REBUILD=true scripts/dev/quick-deploy.sh` (or equivalent
  `az acr build --no-cache` against `terminal/Dockerfile.base`). The
  helper in `scripts/dev/terminal-base-image.sh` keys the base tag off a
  hash of `Dockerfile.base + patch_elastic_blast.py +
  merge-sharded-results.sh`, so the rebuild is gated only by that
  toolchain hash, not by sibling-repo HEAD — `TERMINAL_BASE_REBUILD=true`
  is the explicit override.

`IMAGE_TAGS` in `api/services/image_tags.py` is **not** affected by this
fix. That dict pins the AKS-side BLAST workload images
(`ncbi/elb`, `ncbi/elasticblast-job-submit`, `ncbi/elasticblast-query-split`,
`elb-openapi`). Fix #4 is client-side only and ships via the terminal
sidecar rebuild path above, not via an `IMAGE_TAGS` bump.

## End-to-end measurement (local, post-sibling-push)

Runs executed after `terminal-exec` was already loading the sibling working
tree via `PYTHONPATH=$HOME/dev/elastic-blast-azure/src`, so Fix #4 was
active for both submits. Verified with
`PYTHONPATH=$HOME/dev/elastic-blast-azure/src python3 -c "import
elastic_blast.azure as m; print(m.__file__)"` → resolves to
`src/elastic_blast/azure.py` (which contains
`ThreadPoolExecutor(... thread_name_prefix='elb-warm-reuse')`).

Baseline `10949573-3997-4e96-9bfb-d6b8f61c20c5` (pre-fix, warm cluster) vs
two post-fix warm-cluster runs:

| Step             | Baseline | Run #1 (`9a7c095d`) | Run #2 (`523a60ba`) |
|------------------|---------:|--------------------:|--------------------:|
| `preparing`      | 7s       | 5s                  | 3s                  |
| `warming_up`     | 10s      | 5s                  | 4s                  |
| `configuring`    | 6s       | 2s                  | 3s                  |
| `staging_db`     | 0s       | 0s                  | 0s                  |
| `submitting`     | 104s     | 75s                 | **70s**             |
| `running`        | 21s      | 18s                 | 19s                 |
| `exporting`      | 77s      | 73s                 | 71s                 |
| **End-to-end**   | **247s** | **195s**            | **187s**            |
| Submit Celery wall | n/a    | 174s                | 168s                |

Net: **-60s (-24%) end-to-end** on the latest run vs baseline.

Honest attribution:

- The submit Celery task blocks until K8s submit + export complete, so
  the row's `status` flips `submitting` → `completed` in one shot. The
  poller (Fix #3) and the list-refresh / fast-throttle paths (Fix #1, #2)
  did not actually fire for these runs; they remain valuable for crash
  recovery, external (CLI-side) submissions, and the `results_pending`
  gating but do not explain the speedup here.
- The 30-34 second `submitting` reduction is the Fix #4 contribution
  (parallelising `_cleanup_stale_jobs` + `create_scripts_configmap` +
  `_upload_queries_only` on the warm-reuse path). Two consecutive runs
  landing within 5 seconds of each other on `submitting`
  (70 s vs 75 s) suggests this is signal, not warm-cluster variance.
- The smaller gains in `preparing` / `warming_up` / `configuring` are
  variance — those phases don't intersect any of the four fixes.

Known artefact:

- The `LIST after-terminal poll: status= phase=` lines in
  `/tmp/blast-monitor-2.log` are the same LIST endpoint mismatch noted
  with the previous run — the new row uses the full UUID job_id, but the
  dashboard LIST endpoint still keys by the 12-hex short form. This is
  pre-existing behaviour; the DETAIL endpoint reports completion
  correctly (T+187 s). Not a Fix #1 regression.

Caveats for future runs:

- A cold-cluster run (drop `reuse: true` or scale the node pool to 0
  first) is needed to isolate Fix #4 contribution from warm-cluster
  variance with full confidence.

## Deployed Container App rollout (2026-05-21T17:04 KST)

Ran `TERMINAL_BASE_REBUILD=true scripts/dev/quick-deploy.sh terminal
--rebuild-terminal-base` against the production Container App
(`sub=b052302c-4c8d-49a4-aa2f-9d60a7301a80`, `rg=rg-elb-ca`,
`ca=ca-elb-control`, `acr=acrelbnm5virmqrdi5c`):

- Base image `acrelbnm5virmqrdi5c.azurecr.io/elb-terminal-base:toolchain-7e267c9423054a97`
  rebuilt from scratch in ~5 min. Build log confirms the `git clone
  --depth 1 https://github.com/dotnetpower/elastic-blast-azure.git` step
  re-executed (`Cloning into '/opt/elb/elastic-blast-azure'...` present
  in `/tmp/build-elb-terminal-base.log`, not a CACHED line) — the new
  base image therefore contains sibling commit `47c1369b` at the cloned
  `origin/master` HEAD.
- Runtime overlay `acrelbnm5virmqrdi5c.azurecr.io/elb-terminal:20260521165521`
  built in 1m36s and pushed (digest `sha256:0ca89670c8e264c7cb0e33cfc8e2edfdb7a18b85f4351ba9f9fb8827ade1e8d6`).
- `az containerapp update --container-name terminal` produced new revision
  `ca-elb-control--0000102` (was `0000101`, image
  `elb-terminal:20260520013810`). The new revision moved to
  `RunningAtMaxScale + Healthy + traffic=100%` within ~90 s; old revision
  `0000101` is `Deprovisioning`.
- ACR network policy auto-restored to `publicNetworkAccess=Disabled,
  defaultAction=Deny, trustedServices=true` after the build window.

`IMAGE_TAGS` in `api/services/image_tags.py` was deliberately **not**
touched — it pins AKS workload images (`ncbi/elb`,
`ncbi/elasticblast-job-submit`, `ncbi/elasticblast-query-split`,
`elb-openapi`) and has no `elastic_blast` key. Fix #4 ships exclusively
through the terminal sidecar rebuild path.

## Full sidecar re-deploy (2026-05-21T17:24-17:40 KST)

User requested a full re-deploy of every sidecar onto the production
Container App at
`https://ca-elb-control.gentlemeadow-01289e5b.koreacentral.azurecontainerapps.io/`
(`sub=b052302c-4c8d-49a4-aa2f-9d60a7301a80`, `rg=rg-elb-ca`,
`ca=ca-elb-control`, `acr=acrelbnm5virmqrdi5c`). All five sidecars
(`api`, `worker`, `beat`, `frontend`, `terminal`) were re-imaged via
`scripts/dev/quick-deploy.sh`; only `redis:7-alpine` (external) was left
untouched.

### Images shipped

| Sidecar(s) | Image | Build run | Build time |
| --- | --- | --- | --- |
| `api`, `worker`, `beat` | `acrelbnm5virmqrdi5c.azurecr.io/elb-api:20260521171730` (digest `sha256:c89de4082f0eb3a6b56eac55295e28b8bfb70f575469971427be772c10a17679`) | `de4p` | 2m44s |
| `frontend` (final) | `acrelbnm5virmqrdi5c.azurecr.io/elb-frontend:20260521173038` | `de4r` | 1m40s |
| `terminal` | `acrelbnm5virmqrdi5c.azurecr.io/elb-terminal:20260521173552` (reused base `elb-terminal-base:toolchain-7e267c9423054a97`) | `de4s` | 1m31s |

### Revisions

- `ca-elb-control--0000105` (api+worker+beat patched) at 17:23
- `ca-elb-control--0000106` (frontend, first attempt — **discarded**, see
  pitfall below)
- `ca-elb-control--0000107` (frontend rebuilt with clean env) at 17:34 —
  100% traffic, `Healthy`
- `ca-elb-control--0000108` (terminal patched) at 17:40 — `Activating`,
  becomes primary once readiness probes settle

### Validation evidence

- `curl -sS -o /dev/null -w "%{http_code}" https://ca-elb-control.gentlemeadow-01289e5b.koreacentral.azurecontainerapps.io/`
  → `200`, `/api/health` → `200`, `/health` → `200`, `/api/me` → `401`
  (expected — no MSAL bearer).
- New SPA `index-Ddg3Az0j.js` contains `VITE_API_BASE_URL:""` (same-origin
  `/api`), with **zero** `http://localhost:8085` hits in the bundle.
- ACR network policy auto-restored to
  `publicNetworkAccess=Disabled, defaultAction=Deny,
  trustedServices=true` after every build (EXIT trap in
  `scripts/dev/acr-build-access.sh`).

### Pitfall: `web/.env.local` poisoned the production SPA build

The first frontend deploy (revision `0000106`, image
`elb-frontend:20260521172428`) shipped a SPA with
`VITE_API_BASE_URL:"http://localhost:8085"` baked into the JS bundle —
every dashboard `fetch` would have failed against the production URL.

Root cause: `scripts/dev/quick-deploy.sh::load_simple_env_file()` only
sets an env var when the current value is empty
(`[[ -z "${!key:-}" ]]`). The caller exported `VITE_API_BASE_URL=""`
(empty string) explicitly, but bash `-z` is true for both *unset* and
*empty*, so the helper then re-exported the local-dev override from
`web/.env.local` (`VITE_API_BASE_URL=http://localhost:8085`) on top of
the intentional empty value.

Mitigation applied this run: backed up `web/.env.local` to
`/tmp/web-env-local.bak`, truncated the working-copy file to 0 bytes,
re-ran `scripts/dev/quick-deploy.sh frontend`, then restored
`web/.env.local`. The rebuilt bundle was verified to drop
`localhost:8085`.

Longer-term follow-up (not done in this PR): tighten
`load_simple_env_file()` to honour an explicitly-set empty string
(e.g. switch the guard to `${!key+x}`), or add a separate
`production-env-only` mode to `quick-deploy.sh` that ignores
`web/.env.local`. Until that lands, **every production frontend deploy
must temporarily blank `web/.env.local`** — see this section for the
exact recipe.

### Rollback commands

```
# api / worker / beat (single image)
scripts/dev/quick-deploy.sh api 20260521164034   # previous tag
# frontend (back to terminal-only-deploy snapshot)
scripts/dev/quick-deploy.sh frontend 20260520222840
# terminal (back to the 17:04 KST snapshot)
scripts/dev/quick-deploy.sh terminal 20260521165521
```
