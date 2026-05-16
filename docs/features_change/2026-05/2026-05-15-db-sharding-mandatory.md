# 2026-05-15: Mandatory DB sharding for warmed BLAST databases

## Motivation

The v3 sibling-repo benchmark (see `dotnetpower/elastic-blast-azure`,
`benchmark/strategies/db_prep.py` + report §3-§4.1) demonstrated:

1. Sharding a warmed BLAST DB across N volumes that map 1:1 onto N AKS
   nodes yields up to **11.8x speedup** vs. the non-sharded baseline
   (E16s_v5 × 5 nodes scanning a 269 GB `core_nt`: 31-40 s vs. 533 s).
2. The slow path is triggered by **memory pressure**: when the per-shard
   working set exceeds ~50 % of node RAM, BLAST starts evicting page
   cache and the scan time grows super-linearly.

So far our control plane allowed callers to opt **into** sharding via
free-form `db_partitions` / `db_partition_prefix` INI fields. In practice
no submit was ever sent with those fields populated, so the dashboard
silently delivered the slow path even after the operator had run a
warmup. We now **make sharding the default for any warmed DB** and only
allow opt-out as an explicit boolean.

## User-facing change

* New top-bar pills `elbacr01` / `elbstg01` were removed from
  `web/src/components/ConfigBar.tsx`. The same identifiers are visible in
  the Settings drawer; the pills duplicated information without adding
  value.
* The BLAST Databases modal row now shows a small inline
  `Sharded · N layouts` chip (variant A from the design review at
  `web/public/db-shard-mockups.html`) on every downloaded DB whose
  `metadata.json` carries `sharded: true`. The chip is rendered next to
  the existing `v:NCBI…` code chip on the meta line, uses the
  `--accent` color at 10 % surface, and surfaces the full preset list
  (`Pre-built shard layouts: N = 1, 2, 3, 4, 5, 6, 8, 10. Auto-selected
  per submit based on cluster size & RAM.`) via `title=`. Submit-form
  cluster preview ("Auto-pick N=X for current cluster") and the
  `disable_sharding` opt-out checkbox are scheduled in the next
  follow-up.
* The BLAST submit form's Performance card now shows an
  `Auto-shard · N=X` preview chip below the existing checkboxes
  whenever the selected DB is sharded **and** a cluster is selected.
  The chip is computed by a new client-side helper
  `web/src/utils/dbSharding.ts::selectPartitionsForSubmit`, which mirrors
  the Python helper bit-for-bit so the SPA preview matches what the
  backend will pick. The chip is followed by a low-prominence
  `Disable sharding (advanced …)` checkbox; ticking it strikes through
  the preview and sends `disable_sharding: true` in the submit payload
  so `generate_config` skips the auto-injection.

## API / IaC diff summary

### `api/services/db_sharding.py` (new module, ~470 lines)

Pure-Python helpers for the local-SSD shard layout consumed by the
sibling repo's `init-db-shard-aks.sh`:

* `PRESET_SHARD_SETS = (1, 2, 3, 4, 5, 6, 8, 10)` — covers E16 × {3..10}
  default cluster sizes.
* `MAX_SHARDS = 32`, `SAFE_SHARD_FRACTION_OF_NODE_RAM = 0.5`.
* `list_db_volumes(...)`, `derive_volumes_from_keys(...)` — discover
  volume base names from blob storage or NCBI key listings, using
  `.nsq`/`.psq` as the marker (alias `.nal`/`.pal` is **never** treated
  as a volume — caught a bug in pre-merge critique).
* `plan_shard_layout(...)` — contiguous-block volume assignment matching
  the sibling v3 algorithm.
* `render_manifest(...)`, `render_nal(...)` — text-only file generators
  matching the formats consumed by `init-db-shard-aks.sh`. No BLAST+
  binary execution required (originally planned but discarded after
  reading the sibling init script).
* `upload_shard_set(...)`, `ensure_shard_sets(...)` — idempotent uploads
  to `blast-db/{N}shards/{db}_shard_{NN}/` with content-equality skip.
* `select_partitions_for_submit(db_total_bytes, num_nodes, machine_type)`
  — picks the smallest preset N that satisfies both the node-parallelism
  floor (`N >= num_nodes`) and the memory floor
  (`N >= ceil(db_gib / (node_ram_gib * 0.5))`).
* `partition_prefix_for(...)` — builds the `db-partition-prefix` URL.

Total storage cost per DB across all preset N: **~50 KB** (alias-only
metadata; the real volume data stays at `blast-db/{db}/`).

### `api/services/blast_config.py`

`generate_config()` now auto-injects `db-partitions` and
`db-partition-prefix` whenever **all** of the following hold:

* The caller did **not** pass `disable_sharding=True`.
* The caller did **not** pass `db_partitions` or `db_partition_prefix`
  explicitly (manual override always wins).
* The route resolved DB metadata into params:
  `db_sharded=True`, `db_total_bytes=<int>`, `db_name=<str>`,
  `storage_account=<str>`.

When auto-sharding fires, `cluster.exp-use-local-ssd` is forced to
`true` because the alternative init script
(`init-db-partitioned-aks.sh`) cannot consume our manifest+`.nal`
layout.

### `api/services/storage_data.py`

`list_databases()` propagates two new fields from each DB's
`{db}-metadata.json` blob:

* `sharded: bool` — defaults to `False`.
* `shard_sets: list[int]` — sorted unique preset N values that have a
  complete alias set on storage. Defaults to `[]`.

`total_bytes` from metadata.json now overrides the per-blob enumeration
value when it is present and positive.

### `api/routes/storage.py` (`POST /api/blast/prepare-db`)

After the NCBI key enumeration completes (and copies have been queued),
the `_do_copies()` background thread also derives volume names via
`derive_volumes_from_keys()` and uploads every preset shard set via
`upload_shard_set()`. The shard text files reference volume names that
may not be fully copied yet — that is fine because the AKS init script
downloads the volumes lazily at job runtime.

The `{db}-metadata.json` blob now carries
`sharded: bool` + `shard_sets: list[int]` based on which presets
succeeded.

### `api/tasks/blast.py` (auto-resolution wire-up)

`_build_config_content()` now resolves `{db}-metadata.json` from the
workload Storage account whenever the caller did not pre-populate
`db_sharded` in `options`. The resolver:

* Calls a new `_extract_db_name(database)` helper that handles every
  shape of the `database` field (bare name, `blast-db/<db>`,
  `blast-db/<db>/<db>`, full HTTPS URL).
* Reads `<db>-metadata.json` via the existing `_blob_service` helper.
* Best-effort: any error returns `None` and submit proceeds without
  auto-sharding (no submit failure from a missing metadata blob).

This means the SPA does **not** need to know anything about sharding —
every submit hitting `POST /api/blast/submit` gets the right behaviour
automatically.

### `web/src/components/ConfigBar.tsx`

Removed the read-only `acrName` / `storageAccountName` pills (lines
99-107). The Settings drawer remains the single source of truth for
those values.

## Validation evidence

* `uv run pytest -q api/tests` → **207 passed** (137 baseline + 70 new
  tests for `db_sharding`, `blast_config` auto-shard injection, and
  `_build_config_content` metadata wire-up).
* `uv run ruff check api/services/db_sharding.py api/tests/test_db_sharding.py api/tests/test_blast_config_sharding.py`
  → clean (only pre-existing B008/I001/RUF100 warnings in unmodified
  files remain, none introduced by this change).
* New tests cover:
  * Path-traversal / hostile input rejection in `_validate_db_name`
    (rejects `../etc/passwd`, `core nt`, `core_nt;rm`, `.hidden`, etc.).
  * Contiguous-block layout with remainder absorption.
  * Manifest / `.nal` text format byte-equal to the sibling init script
    contract.
  * `select_partitions_for_submit` for E16/E32/E64 SKUs and 1..10 nodes,
    matching the v3 design.
  * `derive_volumes_from_keys` correctly ignoring alias files (caught a
    high-severity pre-merge bug where `.nal` was being treated as a
    volume).
  * `upload_shard_set` idempotency.
  * `ensure_shard_sets` skips presets that exceed the volume count
    (small / single-volume DBs).
  * Auto-injection respects manual overrides and `disable_sharding`.

## Out of scope (follow-up commits)

* Frontend DB card 3-state UI (warmup / ready / re-warmup) and the
  Submit form info box + Advanced opt-out checkbox.
* `api/tasks/benchmark.py` runner and the `/api/benchmark/*` admin
  routes.
* SPA `BenchmarksPage`.
