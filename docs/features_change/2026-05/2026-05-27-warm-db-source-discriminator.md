# Distinguish explicit warmup from prior-submit DB cache

## Motivation
The New Search "Run profile" picker was auto-selecting **Warmed database**
for DBs that the researcher had never explicitly warmed (e.g. a fresh
`16S_ribosomal_RNA` test). Root cause: `k8s_warmup_status` flagged a DB
as `Ready` whenever an ElasticBLAST `init-ssd-*` setup Job from a prior
BLAST submit had succeeded. The frontend then forced
`enable_warmup = true` via `reconcileShardingSelection`, which activates
the Warmed-database profile button.

The cache is real (init-ssd does stage the DB onto node SSDs), but
treating that as "user warmed it" breaks the mental model — the user
wants the picker to reflect their intent, not incidental side effects of
earlier submits.

## User-facing change
- New Search → Run profile no longer auto-selects **Warmed database**
  unless an explicit dashboard warmup Job (`app=db-warmup`) or
  DaemonSet has run for the selected DB.
- DBs cached only by a prior submit's `init-ssd-*` Jobs still appear in
  the warmup status payload (other UIs like ClusterItem chips keep
  showing them), but the New Search picker ignores them for auto-select.
- No change to the explicit Warmup action or the Sharded-throughput
  path; both continue to gate on the same readiness signals as before.

## API / IaC diff summary
- `GET /api/monitor/aks/warmup-status` response: each `databases[]`
  entry now carries `sources: ("setup" | "warmup")[]`.
  - `setup` — derived from `init-ssd-*` submit-side stager Jobs.
  - `warmup` — derived from `app=db-warmup` Jobs or DaemonSets.
  - Entries that match both union the tags.
- `WarmupDbInfo` TS type extended with the new `sources` field; existing
  consumers are unaffected (field is optional).
- No Bicep / infra changes.

## Files touched
- `api/services/k8s/monitoring.py` — tag setup / daemonset origins and
  union sources in `_merge_database_statuses`.
- `api/services/warmup/jobs.py` — tag warmup-Job origin.
- `web/src/api/monitoring.ts` — add `sources` to `WarmupDbInfo`.
- `web/src/pages/blastSubmit/useWarmupStatus.ts` — only include
  `sources.includes("warmup")` entries in the `warmDbs` map.
- `api/tests/test_k8s_warmup_status_parallel.py` — three new tests
  covering setup-only, warmup-only, and merged-source cases.
- `api/tests/test_warmup_jobs.py` — assertion now expects the
  `sources: ["warmup"]` tag on warmup-Job-derived entries.

## Validation
- `uv run pytest -q api/tests/test_warmup_jobs.py api/tests/test_k8s_warmup_status_parallel.py` → 29 passed.
- `uv run pytest -q api/tests/ -k "warmup or warm_up"` → 104 passed.
- `uv run ruff check api/services/k8s/monitoring.py api/services/warmup/jobs.py` → clean.
- `cd web && npm run build` → succeeded.
