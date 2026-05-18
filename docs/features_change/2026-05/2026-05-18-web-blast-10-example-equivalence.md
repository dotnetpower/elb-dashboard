# Web BLAST 10-Example Equivalence Harness

## Motivation

The bundled BLAST submit examples were expected to match the 10 FASTA benchmark files used for NCBI Web BLAST vs sharded ElasticBLAST equivalence checks. The UI only shipped 9 examples, and the existing EQ14 strict Web XML oracle runner was hard-coded to one MPXV FASTA.

## User-Facing Change

- The BLAST submit examples now include all 10 benchmark FASTA files, including `SARS-CoV-2_orf1ab_NC_045512.2.fasta`.
- The bundled FASTA text now byte-for-byte matches the sibling benchmark source set.
- Query example tests now enforce the exact 10-file source set and verify declared sequence lengths from the FASTA body.
- The New Search page now submits unsharded databases as unsharded even if stale database metadata reports `sharded: true` with only a single shard set.
- The BLAST job detail page now shows result files as soon as output blobs exist, even if the stored dashboard phase is still `submitted`.

## API / IaC Diff Summary

- No production IaC surface changed.
- `scripts/dev/eq14-core-nt-webxml-sharded.sh` is now parameterized by query ConfigMap key, taxonomy filter, Entrez query, Web database, and candidate-pool size.
- Added `scripts/dev/eq15-core-nt-webxml-example-suite.sh` to prepare the 10-query AKS runner ConfigMap and launch strict-oracle jobs one example at a time.
- The EQ14/EQ15 sharded validation path is explicitly `core_nt`-only. It refuses non-`core_nt` databases so unsharded databases are never accidentally run through the 10-shard layout.
- Backend submit config generation now suppresses stale sharding options when Storage metadata says the selected database has no prepared shard layout.
- The BLAST submit UI now applies the same prepared-shard check to preflight and submit payloads. If a restored draft still says `precise` but the selected database has no `shard_sets`, the browser sends `sharding_mode=off`, `db_auto_partition=false`, and no `shard_sets`.
- `scripts/dev/compare-blast-web-xml-outfmt6.py` JSON reports now include first missing accession samples for `web_only` and `candidate_only` cases.
- `scripts/dev/aks-equivalence-runner.sh job-down` now removes the generated `*-user-script` ConfigMap as well as the main script ConfigMap.
- `scripts/dev/docker-compose.full.yml` now passes the Azurite table/blob endpoints to the API, worker, and beat sidecars, and mounts the host Azure CLI cache into the terminal sidecar for local exec auth.
- `terminal/exec_server.py` now forwards an explicit child process Azure auth environment so `azcopy`, `kubectl`, and `elastic-blast` subprocesses do not depend on interactive shell profile state.
- `terminal/patch_elastic_blast.py` now patches the upstream AKS templates for the local-SSD path with `workload=blast` tolerations/node selectors, unique init-SSD job names per ElasticBLAST job suffix, and init wait selectors filtered by `elb-job-id`.
- The BLAST results state loader now falls back to Azure settings stored in the job payload when the result URL has no query string.

## Validation Evidence

- `cd web && npm run test -- src/pages/blastSubmit/queryExamples.test.ts` — passed, 2 tests.
- `cd web && npm run build` — passed. Vite emitted the existing large chunk warning.
- `uv run pytest -q api/tests/test_compare_blast_web_xml_outfmt6.py` — passed, 6 tests.
- `uv run pytest -q api/tests/test_blast_tasks.py api/tests/test_compare_blast_web_xml_outfmt6.py` — passed, 73 tests.
- `uv run ruff check scripts/dev/compare-blast-web-xml-outfmt6.py api/tests/test_compare_blast_web_xml_outfmt6.py` — passed.
- `cd web && npm run test -- src/pages/blastSubmit/taxonomyFilter.test.ts src/pages/blastSubmit/shardingAvailability.test.ts` — passed, 16 tests.
- `bash -n scripts/dev/aks-equivalence-runner.sh scripts/dev/eq14-core-nt-webxml-sharded.sh scripts/dev/eq15-core-nt-webxml-example-suite.sh` — passed.
- `EQ14_DB_NAME=18S_fungal_sequences bash scripts/dev/eq14-core-nt-webxml-sharded.sh dummy.fa` — rejected with exit code 2 before workspace side effects.
- `scripts/dev/eq15-core-nt-webxml-example-suite.sh prepare` — created `elb-equivalence/eq14-core-nt-webxml-tools` with 10 benchmark FASTA files.
- Live AKS/Web BLAST strict-oracle run for `mpxv-f3l-nc-003310`:
  - First run with `MAX_TARGET_SEQS=5000`: strict oracle produced 498/500 shared accessions, no value mismatches.
  - Second run with `MAX_TARGET_SEQS=50000`: still 498/500 shared accessions, no value mismatches.
  - Missing Web accessions were stable across runs: `PZ346335.1`, `PZ346334.1`.
  - `blastdbcmd` probe across all 10 warmed `core_nt_shard_00..09` databases skipped both accessions on every shard, proving the remaining mismatch is DB snapshot drift rather than merge/order/candidate cutoff logic.
- Local six-sidecar browser validation on `http://127.0.0.1:18080` submitted all 10 New Search examples from the browser and confirmed `.out.gz` plus `SUCCESS.txt` artifacts for every job:

| Example | Job ID | Database | Aggregate |
| --- | --- | --- | --- |
| `P. falciparum 18S - chr1` | `294e4b99-0351-4ab3-855f-839af613362a` | `18S_fungal_sequences` | `ok`, 299 hits |
| `MPXV F3L - NC_003310.1` | `7c9f1547-1d41-492d-a099-fdc6a1206e82` | `elb_compare_tiny` | `no_hits`, 0 hits |
| `MPXV F3L - NC_063383.1` | `bdf595c1-6569-49ce-a8e8-785fa5aa177e` | `elb_compare_tiny` | `no_hits`, 0 hits |
| `P. falciparum 18S - chr5` | `22a1731e-cf4f-4211-bb84-b2039fbdfc1d` | `18S_fungal_sequences` | `ok`, 344 hits |
| `P. falciparum 18S - chr7` | `de30dbbb-0bbb-4602-a9f5-f69ea361b0a7` | `18S_fungal_sequences` | `ok`, 344 hits |
| `P. falciparum 18S - chr13` | `35a39944-92e1-40e6-9bbf-5a6a39c30322` | `18S_fungal_sequences` | `ok`, 297 hits |
| `P. falciparum 18S - chr11` | `cdd565ca-6850-4949-80df-150b93cafd28` | `18S_fungal_sequences` | `ok`, 297 hits |
| `SARS-CoV-2 N gene` | `bcad1cd8-761b-44bf-a608-c88d22bfaacb` | `elb_compare_tiny` | `no_hits`, 0 hits |
| `SARS-CoV-2 RdRP` | `9d8e19f7-5ead-418f-ba99-01a674e70ad8` | `elb_compare_tiny` | `no_hits`, 0 hits |
| `SARS-CoV-2 ORF1ab` | `495ef1d3-7690-483f-a05f-0b726b6db8f9` | `elb_compare_tiny` | `no_hits`, 0 hits |

- Browser evidence: the BLAST results page for `22a1731e-cf4f-4211-bb84-b2039fbdfc1d` rendered the result table while job state still read `submitted`, including `batch_000-blastn-18S_fungal_sequences.out.gz` and `SUCCESS.txt`.
- AKS evidence: repeated browser-suite submits created unique `init-ssd-<suffix>-N`, `submit-jobs-<suffix>`, `elb-finalizer-<suffix>`, and `blastn-batch-...-<suffix>` jobs without name collisions or stale init selector failures.

## Follow-Up Required

Exact 500/500 equivalence for the current NCBI Web BLAST `core_nt` result requires refreshing the workload `core_nt` database snapshot from NCBI, regenerating shard manifests, and warming the 10-shard AKS node cache before rerunning the 10-example suite.

The MPXV and SARS bundled UI examples currently target the tiny local comparison database, so their browser-suite validation proves pipeline completion and artifact rendering rather than biological hits. Viral hit validation should be repeated against refreshed `core_nt` after the database snapshot refresh.
