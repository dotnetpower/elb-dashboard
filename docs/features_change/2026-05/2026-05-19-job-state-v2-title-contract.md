# Job State V2 Title Contract

## Motivation

Job lists were inconsistent because the Table row only stored compact state and a
large `payload_json`. Dashboard cards, the Jobs page, and external OpenAPI rows
rebuilt display labels with different fallback rules, so the same submission
could appear under different names.

## User-Facing Change

All job list surfaces now use the canonical `job_title` as the primary row
label. Query filename and DB remain available as secondary context, but the
visible job title is shared across the AKS card, dashboard BLAST Jobs card, and
Jobs page.

## API / IaC Diff Summary

- Added `schema_version=2` job metadata columns to `JobState` table entities:
  `job_title`, `program`, `db`, `query_label`, `subscription_id`,
  `resource_group`, `cluster_name`, and `storage_account`.
- Moved Auto warm preferences out of `jobstate` into a dedicated `autowarmup`
  table so the job table contains only job rows.
- Added a shared `canonical_job_metadata()` helper so new Table rows and
  external OpenAPI rows use the same title/query/db derivation.
- Updated backend job listing to prefer the top-level v2 columns over legacy
  payload fallbacks.
- Updated frontend shared job mapping so every list surface displays
  `job_title` as the primary label.
- Existing `jobstate` / `jobhistory` data may be cleared; the job tables are
  schemaless and will be recreated with v2 columns on the next submission.
  Existing Auto warm preferences are recreated in `autowarmup` by the setup
  flow / reconciler rather than living beside jobs.

## Validation Evidence

- `uv run pytest -q api/tests/test_state_repo.py api/tests/test_external_blast_api.py api/tests/test_blast_tasks.py`
- `uv run pytest -q api/tests/test_auto_warmup.py`
- `uv run ruff check api/services/state_repo.py api/routes/stubs.py api/tests/test_state_repo.py api/tests/test_external_blast_api.py`
- `cd web && npm test -- --run src/components/cards/ClusterBento/jobMapping.test.ts`
- `cd web && npm run build`
