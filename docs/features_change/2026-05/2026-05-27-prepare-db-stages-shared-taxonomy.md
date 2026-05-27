# Stage NCBI shared taxonomy files during prepare-db

Date: 2026-05-27

## Motivation

`elastic-blast`'s warmup script (`init-db-shard-aks.sh` and the in-pod
`warmup_shell_command()`) ships with an `azcopy --include-pattern` that
explicitly looks for the snapshot-root taxonomy files in every per-DB
folder:

```
PATTERN="${PATTERN};taxdb.btd;taxdb.bti;taxonomy4blast.sqlite3;..."
```

…but `prepare-db` was only listing keys with prefix `{latest_dir}/{db_name}`,
so the shared files (`taxdb.btd`, `taxdb.bti`, `taxonomy4blast.sqlite3`)
were never copied into `blast-db/<db>/` on the workload Storage account.
The warmup script's pattern matched nothing and the script logged
`TAXDB_SKIP taxdb files not present in DB prefix`, leaving the pod with
no taxonomy data.

Verified on the live workload Storage account:

```bash
$ az storage blob list --account-name stelbdashboard3abp67bppe \
    --container-name blast-db --prefix '16S_ribosomal_RNA/' --auth-mode login \
    --query "[].name" -o tsv | sort
16S_ribosomal_RNA/16S_ribosomal_RNA-nucl-metadata.json
16S_ribosomal_RNA/16S_ribosomal_RNA.ndb
16S_ribosomal_RNA/16S_ribosomal_RNA.nhr
16S_ribosomal_RNA/16S_ribosomal_RNA.nin
16S_ribosomal_RNA/16S_ribosomal_RNA.nnd
16S_ribosomal_RNA/16S_ribosomal_RNA.nni
16S_ribosomal_RNA/16S_ribosomal_RNA.nog
16S_ribosomal_RNA/16S_ribosomal_RNA.nos
16S_ribosomal_RNA/16S_ribosomal_RNA.not
16S_ribosomal_RNA/16S_ribosomal_RNA.nsq
16S_ribosomal_RNA/16S_ribosomal_RNA.ntf
16S_ribosomal_RNA/16S_ribosomal_RNA.nto
# no taxdb.* / taxonomy4blast.sqlite3
```

NCBI publishes all three at the snapshot root:

```
taxdb.btd                 HTTP 200, 175,546,903 bytes
taxdb.bti                 HTTP 200,  18,330,672 bytes
taxonomy4blast.sqlite3    HTTP 200,  93,450,240 bytes
```

User-facing consequence: `blastn -outfmt '... staxid ssciname scomname
sblastname'` returned `N/A` for the taxonomy columns and v4 DBs had no
taxonomy lookup path at all.

## User-facing change

`prepare-db` now also stages the three snapshot-root taxonomy files into
`blast-db/<db>/` so the warmup script finds them inside the same folder
its existing `--include-pattern` already looks in. No warmup-script or
operator change required; the next prepare-db / re-prepare cycle backfills
existing DBs.

The mechanism is gated by an env flag `PREPARE_DB_INCLUDE_TAXONOMY`
(default `true`); set to `false` to skip — useful only if the workload's
`blastn` invocations never request taxonomy columns and the dataset is
v5-only.

NCBI HEAD-probe failures are non-fatal: a 5xx / 403 on the taxonomy probe
is logged and the rest of the DB still goes through. Per-file 404 is also
tolerated (NCBI occasionally drops `taxonomy4blast.sqlite3` while
regenerating).

## API / IaC diff summary

* [api/routes/storage/common.py](../../../api/routes/storage/common.py)
  * New constant `SHARED_TAXONOMY_FILES = ("taxdb.btd", "taxdb.bti", "taxonomy4blast.sqlite3")`.
  * New public function `shared_taxonomy_keys(latest_dir)` — HEAD-probes each
    file, returns the existing ones as `{latest_dir}/<name>` keys.
    Honours the existing NCBI circuit breaker; empty results are
    intentionally **not** cached so a transient outage does not poison
    the next hour.
  * `reset_ncbi_catalogue_cache()` now also clears the new
    `_SHARED_TAXONOMY_KEYS_CACHE` dict.
* [api/routes/storage/prepare_db.py](../../../api/routes/storage/prepare_db.py)
  * New env flag `_INCLUDE_SHARED_TAXONOMY` (env: `PREPARE_DB_INCLUDE_TAXONOMY`,
    default `true`).
  * After `_list_keys` resolves per-DB keys, append `shared_taxonomy_keys(latest_dir)`
    to the copy plan. Per-file destination is `blast-db/<db>/<basename>` via
    the unchanged `_copy_one` (file basename → DB folder), so the warmup
    script finds them without any script change.
  * HEAD-probe failure → logged warning, taxonomy skipped, DB-file copy
    still proceeds.
* [api/tests/test_storage_shared_taxonomy.py](../../../api/tests/test_storage_shared_taxonomy.py) (new)
  * Unit tests for the new helper: success, per-file 404 skip, caching,
    empty-result NOT cached, 403 → `NcbiAccessDenied`, 5xx → `NcbiUnavailable`.
  * Route-level tests for the merged copy plan: taxonomy files land at
    `blast-db/<db>/<name>`, feature flag off skips them, probe failure
    is tolerated.

No infra (Bicep) changes. No frontend changes. No new dependencies.

## Validation evidence

```bash
$ uv run pytest -q api/tests/test_storage_shared_taxonomy.py \
                  api/tests/test_storage_common_cache.py \
                  api/tests/test_prepare_db_hardening.py \
                  api/tests/test_prepare_db_routes.py
23 passed in 7.45s

$ uv run pytest -q api/tests/ -k "storage or prepare_db or warmup or ncbi or blast_database"
308 passed in 30.64s

$ uv run ruff check api
All checks passed!
```

Direct workload-Storage verification (before patch) and NCBI HEAD checks
of all three files (HTTP 200) attached above.

## Backfill

Existing prepared DBs do not have the taxonomy files yet. To backfill,
re-run `prepare-db` for each DB (the dashboard's "Update" / Download
action), or run a one-shot azcopy from NCBI snapshot root for each
existing `<db>` folder. Future prepare-db runs are correct by construction.
