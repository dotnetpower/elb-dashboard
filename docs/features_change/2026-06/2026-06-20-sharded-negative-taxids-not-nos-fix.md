# Fix: `-negative_taxids` / `-taxids` filter on sharded core_nt (blastn exit 255)

## Motivation
A live NCBI-parity run on the dev cluster surfaced that any sharded `core_nt`
BLAST submission carrying a **Taxonomy Filter** (Include or Exclude) failed with
blastn exit code 255. Because `core_nt` (262 GB) can only run in sharded mode on
the `Standard_E16s_v5` node pool (the baseline full-DB path is blocked by the
RAM-headroom gate), organism include/exclude — a common NCBI Web BLAST feature
exposed in the dashboard's Taxonomy Filter step — was effectively broken for
`core_nt` on this deployment.

## Root cause (empirically isolated, not guessed)
A diagnostic pod (`ncbi/elb:1.4.0`, blastn 2.17.0+) was run on a `blastpool`
node. It staged shard_09 (volumes `core_nt.81..86`, 28 GB) using the **exact**
`init-db-shard-aks.sh` download pattern, then ran the F3L MPXV query with
`-negative_taxids 3431483 -outfmt 5`:

* **Without `.nos`/`.not`** (faithful current pattern): `EXIT=255`, stderr =
  `Error: (CFileException::eMemoryMap) ... To be memory mapped the file must
  exist: '<db>.not'`.
* **With `core_nt.nos` + `core_nt.not` added**: `EXIT=0`, 19 hits, top hit
  `Cowpox virus isolate CPXV_K4207` — which matches the NCBI reference for F3L
  with the Monkeypox taxon (3431483) excluded.

The shard download pattern fetched the DB-prefix taxonomy files
`.ndb;.ntf;.nto` plus `taxdb.btd/.bti` and `taxonomy4blast.sqlite3`. Those cover
the `staxids`/`sscinames` **output** lookup (which is why `outfmt 7 staxids`
already worked on shards), but the `-taxids`/`-negative_taxids` **filter**
additionally memory-maps the seqid→taxid index `${ORIG_DB}.nos` and
`${ORIG_DB}.not`, which were omitted. The source DB blob already contains both
files at the DB-prefix level.

## User-facing change
Sharded `core_nt` searches with a Taxonomy Filter (Include or Exclude) now run
to completion instead of failing with an unexplained exit 255.

## Code change summary
* `terminal/patch_elastic_blast.py` — append `;${ORIG_DB}.nos;${ORIG_DB}.not` to
  the shard download `PATTERN` in `_HARDENED_INIT_DB_SHARD_AKS_SCRIPT` (the
  source of truth that `patch_init_shard_script` writes wholesale into the
  elb-openapi / terminal images). Added an explanatory comment.
* `terminal/patch_elastic_blast.py` — **self-heal for warm caches staged before
  this fix.** A node-local shard cache carries a `.download-complete` marker and
  is reused verbatim, so simply adding `.nos`/`.not` to the pattern would not
  reach clusters that were already warmed with the old image (their cache keeps
  the marker, lacks `.nos`/`.not`, and the next sharded taxon-filtered search
  still aborts). The script now invalidates `.download-complete` when the OUTPUT
  taxonomy file `${ORIG_DB}.ntf` is present locally but the FILTER index
  `${ORIG_DB}.not`/`.nos` is not — i.e. the cache predates the fix — so the next
  warmup re-stages them. Non-taxonomy DBs (no local `.ntf`) are left untouched.
* `api/tests/test_terminal_patch_elastic_blast.py` — regression guard asserting
  `${ORIG_DB}.nos`/`.not` are in the download pattern and the self-heal
  `CACHE_INCOMPLETE missing taxonomy filter index` branch is present.
* `scripts/dev/patch-openapi-build-context.py` — force `ARG ELB_REF` to the
  known-good `7a471297` via regex (the sibling Dockerfile default drifted to
  `5b7ea2b`, which dropped `bin/elastic-blast` and broke the image build).
* Sibling `dotnetpower/elastic-blast-azure` (authorised by the maintainer):
  `src/elastic_blast/templates/scripts/init-db-shard-aks.sh` — same `.nos`/`.not`
  addition and self-heal block for upstream consistency (the dashboard patch
  overwrites it at image-build time, so this is hygiene rather than the deploy
  path).

## Validation evidence
* `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py` → 21 passed.
* `uv run ruff check terminal/patch_elastic_blast.py api/tests/...` → clean.
* Live diagnostic pod reproduction: RUN A exit 255 (`.not` missing) → RUN B
  exit 0 / 19 hits / top hit Cowpox virus after adding `.nos`/`.not`.

## Deploy note
Takes effect on the live cluster only after an **elb-openapi (+ terminal) image
rebuild and redeploy** — the shard-staging script ships inside those images.
