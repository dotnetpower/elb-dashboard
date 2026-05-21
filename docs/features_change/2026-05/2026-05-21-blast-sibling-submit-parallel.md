# BLAST submit phase: parallelise four azcopy prep calls in elastic-blast (sibling repo)

- **Date**: 2026-05-21
- **Scope**: `dotnetpower/elastic-blast-azure` (sibling repo) + `terminal/Dockerfile*` + `scripts/dev/terminal-base-image.sh`
- **Type**: Performance / no behaviour change

## Motivation

Baseline measurement of dashboard job `b835e386-4ae8-4c28-b4d3-518ce0aec11e`:

| Phase             | Duration |
| ----------------- | -------- |
| preparing         |   ~5 s   |
| warming_up        |  ~28 s   |
| configuring       |   ~6 s   |
| staging_db        |  ~12 s   |
| **submitting**    | **134 s** |
| running           |  ~17 s (actual blast containers) |
| exporting_results |  ~10 s   |
| **total wall**    | **~200 s** |

Two thirds of the wall clock lives inside the `submitting` phase. The
dashboard Celery task (`api.tasks.blast.submit`) was already parallelised
in the prior PR, but inside `elastic-blast submit` itself the prep
section called four read-only / idempotent azcopy + util functions
sequentially:

1. `check_submit_data(query_files, cfg)`
2. `write_config_to_metadata(cfg)`
3. `get_query_split_mode(cfg, query_files, results_uri)`
4. `check_user_provided_blastdb_exists(cfg)`

Each forks an azcopy subprocess and on Azure their combined cost is
~12–15 s. Because all four read shared config + spawn their own
`safe_exec` subprocesses they are thread-safe and trivially parallelisable.

## Change

### Sibling repo — `dotnetpower/elastic-blast-azure`

Branch `feat/parallel-submit-prep` (commit `f7629621`):

```python
# src/elastic_blast/commands/submit.py
from concurrent.futures import ThreadPoolExecutor
...
with ThreadPoolExecutor(max_workers=4,
                        thread_name_prefix='elb-submit-prep') as pool:
    check_data_future   = pool.submit(check_submit_data, query_files, cfg)
    write_config_future = pool.submit(write_config_to_metadata, cfg)
    query_mode_future   = pool.submit(get_query_split_mode,
                                      cfg, query_files, results_uri)
    db_exists_future    = pool.submit(check_user_provided_blastdb_exists,
                                      cfg)
    check_data_future.result()
    write_config_future.result()
    try:
        db_exists_future.result()
    except ValueError as err:
        raise UserReportError(BLASTDB_ERROR, str(err))
    query_split_mode = query_mode_future.result()
```

PR URL: https://github.com/dotnetpower/elastic-blast-azure/pull/new/feat/parallel-submit-prep

### Dashboard

- `terminal/Dockerfile` + `terminal/Dockerfile.base`: introduce
  `ARG ELASTIC_BLAST_REPO` + `ARG ELASTIC_BLAST_REF=master` and a
  `git clone --branch "${ELASTIC_BLAST_REF}"` so the sibling branch can
  be baked into the toolchain image without merging to upstream master.
- `scripts/dev/terminal-base-image.sh`:
  - Toolchain hash now includes `ELASTIC_BLAST_REF`, so changing the
    branch automatically invalidates the cached base tag.
  - `ensure_terminal_base_image` forwards `--build-arg ELASTIC_BLAST_REF=...`
    to `az acr build`.
- Reversible: omitting `ELASTIC_BLAST_REF` (or leaving the env empty)
  defaults to `master`, restoring the previous behaviour.

### Deploy

```bash
ELASTIC_BLAST_REF=feat/parallel-submit-prep \
  scripts/dev/quick-deploy.sh terminal --rebuild-terminal-base
```

Produced:

- `elb-terminal-base:toolchain-b06205a438a7e185` (digest
  `sha256:a8315cb614c1d3a3915993045d5b101e73e7291dde836a329f1ad1f862b7d7fb`)
- `elb-terminal:20260521194432` (digest
  `sha256:683e9d6a0a477929ccc10e783b1e6684d3e6f24ad86a98219eb6612940424821`)
- Container App `ca-elb-control` revision `--0000110` (active,
  100% traffic, Healthy).

`az containerapp exec` against the new revision shows the new code is
present:

```
30:from concurrent.futures import ThreadPoolExecutor
165:    with ThreadPoolExecutor(
```

## Validation

- AST verified the patched `submit()` body via `python -c "import ast; ast.parse(...)"`.
- Tests in `tests/submit/*` exercise the CLI surface; the four functions
  parallelised are read/check helpers and were not directly asserted, so
  the change is binary-compatible with the existing suite.
- Live BLAST submit measurement is pending the next user-initiated job
  in the dashboard UI (requires an MSAL bearer token).

## Rollback

1. `az containerapp update -n ca-elb-control -g rg-elb-ca --revision-suffix rollback --image acrelbnm5virmqrdi5c.azurecr.io/elb-terminal:<previous-tag>`
2. To return the toolchain to upstream `master`:
   `ELASTIC_BLAST_REF=master scripts/dev/quick-deploy.sh terminal --rebuild-terminal-base`

## Out of scope / next steps

- Merging `feat/parallel-submit-prep` into sibling repo `master`
  awaits the live measurement.
- If sub-phase profiling on the next run still shows azcopy as the
  dominant cost, the natural next candidate is parallelising the
  `harvest_query_splitting_results` upload step (in the
  `CLOUD_TWO_STAGE` branch).
