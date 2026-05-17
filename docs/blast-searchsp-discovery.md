# BLAST Search Space Discovery

This note records the small-database discovery used to understand whether NCBI
Web BLASTAlign uses a fixed `searchsp` value. It does not: the inferred effective
search space changes with the query/subject set and BLAST options.

## Question

For sharded ElasticBLAST to match a full database BLAST+ run, every shard must use
the same statistical search space as the full run. BLAST+ exposes that override as
`-searchsp`. The question tested here was whether NCBI Web BLASTAlign uses a fixed
hidden `searchsp`, or whether the value depends on the query and custom subject
database.

## Method

Custom query-vs-subject Web BLAST jobs must be submitted through
`https://blast.ncbi.nlm.nih.gov/BlastAlign.cgi`; `Blast.cgi` rejects this mode and
asks clients to use `BlastAlign.cgi`.

For each synthetic custom subject database:

1. Submit Web BLASTAlign with `PROGRAM=blastn`, `MEGABLAST=off`, `EXPECT=1000`,
   `FILTER=F`, and XML output.
2. Parse Web XML hit e-values and bit scores.
3. Build the same local subject FASTA as a BLAST database using BLAST+ 2.17.0.
4. Run local BLAST+ with the same options and with explicit `-searchsp 1`.
5. Infer the Web/default effective search space per hit:

```text
inferred_searchsp = web_evalue / local_evalue_at_searchsp_1
```

6. Round the inferred value and validate by running local BLAST+ with
   `-searchsp <rounded>`; the local rows should match Web rows exactly.

The helper script is [scripts/dev/ncbi-searchsp-discovery.py](../scripts/dev/ncbi-searchsp-discovery.py).
It calls NCBI Web BLASTAlign and runs local BLAST+ through the terminal image, so
it is an external-network dev probe rather than a unit test.

## Validation Results

Date: 2026-05-16

BLAST version reported by Web XML: `BLASTN 2.17.0+`.

All tested Web XML statistics blocks reported `Statistics_eff-space = 0`,
`Statistics_db-len = 0`, and `Statistics_db-num = 0`, so the Web/default search
space was not directly exposed by XML in this custom subject mode.

| Case | Web RID | Query length | Subject set | Inferred `searchsp` | Per-hit inferred range | Local default equals Web | Local `-searchsp` equals Web |
| --- | --- | ---: | --- | ---: | --- | --- | --- |
| `baseline_32nt_4_subjects` | `0G0J7RM8114` | 32 | 4 synthetic subjects | 2704 | 2703.995..2704.007 | yes | yes |
| `longer_64nt_4_subjects` | `0G0PF0XD114` | 64 | 4 synthetic subjects | 12544 | 12543.964..12544.006 | yes | yes |
| `wider_32nt_8_subjects` | `0G0US0KB114` | 32 | 8 synthetic subjects | 4950 | 4949.983..4950.010 | yes | yes |

Representative Web rows and validated local `-searchsp` rows matched exactly for
e-value and bit score. For example, the baseline case matched:

```text
subject_best  4.7099e-15   58.9941
subject_bit   1.64392e-14  57.1907
subject_slow  6.9901e-13   52.6823
subject_far   2.97227e-11  46.3705
```

The longer 64 nt case produced a different hidden/default search space:

```text
subject_best               9.28242e-32  116.702
subject_one_tail_mismatch  3.23988e-31  114.899
subject_two_block_mismatch 1.6783e-28   105.882
subject_terminal_noise     5.85785e-28  104.078
```

## Conclusion

The effective search space is not a fixed Web BLASTAlign constant. It changes
when the query length or custom subject database changes. For these synthetic
cases, Web BLASTAlign matched local BLAST+ default behavior exactly, and the
default behavior was equivalent to different explicit `-searchsp` values:
`2704`, `12544`, and `4950`.

For sharded BLAST equivalence, the practical rule remains:

- Use a full database baseline, or infer the equivalent full-run `searchsp` from
  a reference run.
- Pass that same `-searchsp` value to every shard run.
- Do not assume a universal NCBI Web BLAST `searchsp` value.

## Large DB / core_nt Calibration Strategy

The small custom-subject discovery above proves that `searchsp` is not a fixed
constant. For a production-sized database such as `core_nt`, the useful
reference is therefore not an NCBI Web BLAST hidden value. The reference should
be a local full-database BLAST+ run using the same BLAST+ version, same database
snapshot, same query set, and same options that the sharded ElasticBLAST run will
use.

The first calibration should be a one-off Azure VM experiment, not a new AKS task.
That keeps the experiment simple, auditable, and easy to delete as soon as the
reference value is captured.

### Completed core_nt Calibration

Date: 2026-05-16

The first full-database calibration completed on a temporary Azure VM and the
temporary resource group was deleted after the evidence archive was copied out.
This value applies to the exact query, database snapshot, BLAST+ version, and
options listed below.

| Field | Value |
| --- | --- |
| Result archive | `docs/temp/core-nt-searchsp/core_nt-searchsp-calibration-results.tgz` |
| Azure VM | `Standard_E96as_v5` in `koreacentral` |
| BLAST+ | `blastn: 2.17.0+`, package `blast 2.17.0`, build `Jul 1 2025 08:59:18` |
| Database | NCBI `core_nt`, BLASTDB version `5`, date `May 2, 2026 1:17 AM` |
| Database size | `125,619,662` sequences; `1,041,443,571,674` total bases |
| Query | `calibration_query_64nt`, length `64` |
| Query SHA-256 | `4c7007e3431bb780ab769516c1a90cc0604dedb9d7e9e9b3e633aa7ac2ea4c51` |
| Options | `-word_size 28 -dust yes -evalue 10 -max_target_seqs 500 -outfmt 5` |
| Threads | `96` |
| Full DB `Statistics_eff-space` | `32156241807668` |

Use `32156241807668` as the shard-wide `-searchsp` value only for the matching
`core_nt` snapshot, query, BLAST+ version, and options. Recalibrate when any of
those inputs change.

### AKS E16 Shard Comparison

Date: 2026-05-16

The full DB value was validated on a real AKS shard path after warming
`core_nt` across 10 E16-class nodes. The requested `Standard_E16s_v5 x 10`
shape was blocked by `koreacentral` ESv5 family quota (`96/100` vCPUs already
in use), so the temporary experiment pool used `Standard_E16s_v3 x 10`, which
has the same 16 vCPU / 128 GiB node class and fit the available ESv3 quota.

The experiment generated 10-shard manifests for `core_nt`, created a temporary
`blastp16v3` AKS node pool, downloaded one shard per node with
`init-db-shard-aks.sh`, and ran `blast-vmtouch-aks.sh` before comparison. The
temporary pool and Kubernetes Jobs were deleted after evidence collection.

The shard totals matched the full DB calibration:

```text
sum_db_len = 1041443571674
sum_db_num = 125619662
```

Each warmed shard then ran the same 64 nt query twice: once with shard-local
defaults, and once with `-searchsp 32156241807668`.

| Shard | Default shard `Statistics_eff-space` | With full DB `-searchsp` |
| --- | ---: | ---: |
| `00` | 3629470027572 | 32156241807668 |
| `01` | 3628761623292 | 32156241807668 |
| `02` | 3629747472502 | 32156241807668 |
| `03` | 3629020951900 | 32156241807668 |
| `04` | 3629322533634 | 32156241807668 |
| `05` | 3629209917406 | 32156241807668 |
| `06` | 3628517936316 | 32156241807668 |
| `07` | 3629896261840 | 32156241807668 |
| `08` | 3629233564780 | 32156241807668 |
| `09` | 2617769092434 | 32156241807668 |

Result: shard-local defaults are smaller and vary by shard, but the calibrated
full DB value is accepted uniformly by all warmed shards. For precise sharded
comparison, pass `-searchsp 32156241807668` to every shard for this exact
query/options/database snapshot.

### Product Default Mapping

Date: 2026-05-17

The dashboard now carries only verified search-space defaults. The mapping is
deliberately database-specific and evidence-scoped; unknown databases do not get
a fabricated value.

| Database | Default `searchsp` | Scope | Evidence |
| --- | ---: | --- | --- |
| `core_nt` | `32156241807668` | `blastn`, `core_nt` 2026-05-09 snapshot, 64 nt calibration query, `-word_size 28 -dust yes -evalue 10 -max_target_seqs 500 -outfmt 5` | `docs/temp/core-nt-searchsp/core_nt-searchsp-calibration-results.tgz` |
| `16S_ribosomal_RNA` | not set | Web RID captured; local full DB and sharded equivalence not completed | `docs/temp/web-blast-equivalence/2026-05-17-16s-carnobacterium/` |
| `18S_fungal_sequences` | not set | Not calibrated against NCBI Web BLAST yet | n/a |
| `ITS_RefSeq_Fungi` | not set | Not calibrated against NCBI Web BLAST yet | n/a |
| `elb_compare_tiny` | not set | Synthetic local equivalence probe only; not an NCBI Web BLAST database | n/a |

Implementation details:

- `api/services/web_blast_searchsp.py` stores the verified defaults in one
  place.
- `/api/blast/databases` adds `web_blast_searchsp` and evidence metadata to the
  matching database row, so the submit UI can show the selected default.
- `/api/blast/pre-flight` and `/api/blast/jobs` apply the default as
  `db_effective_search_space` when the user has not supplied a value.
- An explicit `-searchsp` in `additional_options` remains the override path;
  the automatic default is skipped in that case.

Live validation on 2026-05-17:

- Local Redis was recreated, Celery queues were empty, and the API, worker,
  beat, web, and terminal-exec processes were restarted under
  `.logs/local/20260517T035004Z-1039155/`.
- `GET /api/health/celery` returned registered worker tasks and zero queue
  depth for `default`, `azure`, `blast`, `storage`, and `celery`.
- `POST /api/health/celery/enqueue-noop?message=searchsp-reset-check` returned
  `SUCCESS`, proving task dispatch and result persistence through Redis.
- `api.services.terminal_exec.run(["elastic-blast", "--version"])` returned
  `elastic-blast 1.5.0.post63+e3e9f51`, proving the CLI execution path.
- `GET /api/blast/databases?...storage_account=elbstg01...machine_type=Standard_E16s_v5`
  returned `core_nt.web_blast_searchsp = 32156241807668`,
  `total_letters = 1041443571674`, and shard sets `[1,2,3,4,5,6,8,10]`.
- `POST /api/blast/pre-flight` with the UI-style `aks_cluster_name` payload and
  no explicit `db_effective_search_space` returned `ready: true`; the backend
  injected the `core_nt` default before the sharding precision gate.
- A previously completed real AKS `core_nt` sharded run
  (`job-9b651e1d5bc74ee4a92fa2fb138d8383`) had Kubernetes BLAST Jobs
  `blastn-batch-s00..s09-job-000-138d8383`, all `Complete`. The generated
  ElasticBLAST config contained:

```text
options = -evalue 10 -max_target_seqs 500 -outfmt 5 -word_size 28 -searchsp 32156241807668 -dust yes
db-partitions = 10
db-partition-prefix = https://elbstg01.blob.core.windows.net/blast-db/10shards/core_nt_shard_
```

- A shard XML sample from `shard_00/batch_000-blastn-core_nt_shard_00.out.gz`
  reported `Statistics_eff-space = 32156241807668`, confirming the value reached
  the pod-level BLAST invocation.
- Browser verification on `http://127.0.0.1:8090/blast/submit` selected
  `core_nt` and showed the submit summary
  `E-value: 0.05 · Max: 100 · Fmt: 5 · Searchsp: 32156241807668`.
- Fresh AKS validation job `d07a6a3a-208d-4606-87ff-33304bc7e7dd` submitted
  `core_nt` with precise 10-way local-SSD sharding. ElasticBLAST internal job
  `job-9c58c936101042ea996681485be97da5` generated
  `blastn-batch-s00..s09-job-000-5be97da5`; all 10 shard jobs completed in
  `17s` to `19s`.
- The generated shard job manifest mounted hostPath `/workspace` at
  `/blast/blastdb` with `subPath: blast`, used `ELB_DB=core_nt_shard_00`, and
  passed `ELB_BLAST_OPTIONS=-max_target_seqs 500 -outfmt 5 -word_size 28
  -searchsp 32156241807668 -dust yes`. The only init container was
  `import-query-batches`, which completed in about 2 seconds; there was no DB
  download init container in the warmed local-SSD path.
- The first live finalizer failed after BLAST completed because the mounted
  script still used the invalid azcopy path wildcard
  `${SHARD_DIR}/*.out.gz`. The runtime patch now uses
  `${SHARD_DIR}/* --include-pattern "*.out.gz"` and runs the finalizer in
  `elasticblast-job-submit:4.1.0` so `kubectl` is available.
- After updating ConfigMap `elb-scripts`, deleting the stale finalizer job, and
  recreating it from the same manifest, `elb-finalizer-5be97da5` completed. The
  patched finalizer downloaded 10 shard XML files, merged `0` hits from `1`
  query, uploaded `merged_results.out.gz`, uploaded `merge-report.json`, and
  wrote `metadata/SUCCESS.txt`. A second rerun after XML metadata normalization
  completed in `46s`.
- Final evidence lives under
  `docs/temp/core-nt-searchsp/fresh-2026-05-17/live-finalizer-5be97da5/`:
  `merged_results.out.gz`, `merge-report.json`, `SUCCESS.txt`, `blob-list.txt`,
  `merged_vs_baseline_stats.json`, and `canonical-compare.json`.
- The downloaded merged XML was compared against the full DB baseline XML
  `docs/temp/core-nt-searchsp/extracted/results/core_nt.full.default.xml`.
  `all_result_statistics_match` is `true`: `db_len=1041443571674`,
  `db_num=125619662`, `eff_space=32156241807668`, `hsp_len=33`, `hit_count=0`,
  `hsp_count=0`, `lambda=1.28`, `kappa=0.46`, and `entropy=0.85` all match.
  The merged top-level `BlastOutput_db` is normalized to `core_nt`. The XML is
  not byte-identical to the VM baseline because the baseline records the local
  database path `/mnt/elb-calibration/blastdb/core_nt` and XML serialization is
  produced by the merge helper.
- `scripts/dev/compare-blast-xml.py` was then run against the same full DB
  baseline and sharded merged XML. It reported `equivalent: true`,
  `difference_count: 0`, and ignored only the provenance-only
  `BlastOutput_db` field while normalizing both DB names to `core_nt`.
- A fresh dashboard smoke submit was also attempted for `16S_ribosomal_RNA`
  (`job_id = b9a1c180-06a9-449d-b12f-aefc12ff42bc`). The API uploaded the
  query to `queries/uploads/b9a1c180-06a9-449d-b12f-aefc12ff42bc/query.fa`,
  created the result metadata prefix, and launched `elastic-blast submit` via
  terminal-exec. The run did not reach BLAST pod execution: ElasticBLAST blocked
  while initializing PVC `blast-dbs-pvc-rwm` because the AKS cluster does not
  have StorageClass `azureblob-nfs-premium`.
- The smoke submit was cancelled and the leftover validation resources
  `job/init-pv` and `pvc/blast-dbs-pvc-rwm` were deleted. This is a cluster
  storage-class readiness issue, not a `searchsp` defaulting mismatch.
- The browser job detail page refreshed to `failed / submit_failed` for that
  smoke job and showed the persisted submit error output in the failed step.

Residual risk:

- The verified `core_nt` value is not universal. It is valid for the documented
  database snapshot, query length, BLAST+ version, and option set. Recalibrate
  if any of those inputs change.
- A hit-positive NCBI Web BLAST RID was captured for `16S_ribosomal_RNA`, but
  local full DB and sharded equivalence have not been completed for that RID.
  No default was added for `16S_ribosomal_RNA`.
- NCBI Web BLAST standard-database RIDs for `18S_fungal_sequences` and
  `ITS_RefSeq_Fungi` were not completed in this pass, so no defaults were added
  for those databases.
- Update: the Web BLAST UI value for the 16S database is
  `rRNA_typestrains/16S_ribosomal_RNA`, not the local BLAST DB basename
  `16S_ribosomal_RNA`. Submitting the local basename through QBlast returns a
  syntactically valid XML document with `Statistics_db-len=0` and no hits, so it
  must not be treated as Web evidence.
- The current equivalence evidence is result/statistical equivalence for the
  documented no-hit calibration query, not byte-for-byte XML identity.
- The dashboard job row for `d07a6a3a-208d-4606-87ff-33304bc7e7dd` may still
  show `running` in local state because the finalizer was manually rerun after
  the original worker path had already exited; Kubernetes and Storage evidence
  show the run completed and wrote `metadata/SUCCESS.txt`.

### Action Items to Close Web BLAST Equivalence

The target claim should be **Web BLAST-compatible result equivalence**, not
byte-for-byte XML identity. A run passes only when the compared biological and
statistical fields match exactly after normalizing expected provenance-only
fields such as local database paths and XML serialization.

| ID | Status | Action | Pass criteria |
| --- | --- | --- | --- |
| `EQ-01` | Done for `core_nt` no-hit calibration | Pin the exact BLAST+ version, DB snapshot, query, and options. | Evidence records `BLASTN 2.17.0+`, `core_nt` May 2026 snapshot, query SHA-256, and complete option string. |
| `EQ-02` | Done for `core_nt` no-hit calibration | Capture the full DB baseline search-space statistics. | Full DB XML reports `Statistics_eff-space = 32156241807668`, `db_len = 1041443571674`, and `db_num = 125619662`. |
| `EQ-03` | Done for `core_nt` no-hit calibration | Force every shard to use the full DB search space. | Kubernetes shard manifests include `-searchsp 32156241807668`; shard XML reports the same `Statistics_eff-space`. |
| `EQ-04` | Done for `core_nt` no-hit calibration | Merge shard XML statistics back to full DB statistics. | `merged_vs_baseline_stats.json` reports `all_result_statistics_match: true` for `db_len`, `db_num`, `eff_space`, `hsp_len`, hit count, HSP count, lambda, kappa, and entropy. |
| `EQ-05` | Done for `core_nt` no-hit calibration | Add a canonical BLAST XML comparator script for Web/full/sharded outputs. | `scripts/dev/compare-blast-xml.py` ignores provenance-only fields but compares query IDs, subject IDs/accessions, HSP coordinates, aligned sequences, identity, gaps, e-value, bit score, and output ordering. Existing evidence `canonical-compare.json` reports `equivalent: true` and `difference_count: 0`. |
| `EQ-06` | Partial | Capture at least one NCBI Web BLAST RID with positive hits for each supported database default. | `scripts/dev/test_queries/16S_carnobacterium.fa` was submitted to Web DB `rRNA_typestrains/16S_ribosomal_RNA`; RID `0JXX09HH016` produced 500 hits / 500 HSPs. Evidence is under `docs/temp/web-blast-equivalence/2026-05-17-16s-carnobacterium/`. Web RIDs for `18S_fungal_sequences` and `ITS_RefSeq_Fungi` remain open. |
| `EQ-07` | Partial for 16S | Reproduce each Web RID locally against the same database snapshot. | Local BLAST+ 2.17.0 and the May 14 2026 `16S_ribosomal_RNA` database reproduced the Web DB size and top hit in `2.54s`. Strict canonical comparison still fails: first hit-order mismatch is rank 111, 438/500 hit IDs overlap, and Web XML reports `Statistics_eff-space=0` while local BLAST+ reports `57425628120`. |
| `EQ-08` | Partial for local 16S probe | Run the same hit-positive query through precise local-SSD sharding. | A local 4-shard 16S probe matched full-run DB statistics exactly (`db_len`, `db_num`, `eff_space`, `hsp_len`, `lambda`, `kappa`, `entropy`) but strict hit ordering diverged in tied high-score regions. Round-robin and contiguous shard splits both kept the same top accession and top-10 order, but first full-vs-sharded accession mismatch was rank 15. |
| `EQ-09` | Open | Stress top-N merge behavior near `max_target_seqs` boundaries. | Golden cases cover ties, duplicate subject IDs across shards, multiple HSPs per hit, and more candidate hits than `max_target_seqs`; ordering is deterministic and matches the full DB baseline. |
| `EQ-10` | Open | Validate multi-query and query-group behavior. | Each query group carries the correct full-run search space; merged output preserves per-query statistics and hit ordering. |
| `EQ-11` | Open | Expand verified defaults database by database. | `16S_ribosomal_RNA`, `18S_fungal_sequences`, and `ITS_RefSeq_Fungi` remain unset until `EQ-06` through `EQ-10` pass for their own Web/default evidence. |
| `EQ-12` | Open | Make the dashboard claim evidence-aware. | The UI may show `Web-compatible` only for databases/options with passing evidence; unknown combinations must stay explicit override/manual calibration paths. |
| `EQ-13` | Partial / blocked by snapshot mismatch and top-N ties | Compare NCBI Web BLAST CSV exports against same-snapshot sharded tabular results. | `scripts/dev/compare-blast-web-csv.py` compares Web CSV exports with BLAST outfmt 6 rows by accession, ordering, and value fields. Current MPXV F3L CSV exports do not match either the older sibling v3 benchmark or the fresh `core_nt` AKS run because shared accession overlap is `0`. Recheck showed the Web top accession `OZ461628.1` exists in current shard 05, but it aligns as `98.701%` / bit `821` at rank `1013` when shard 05 is run with `max_target_seqs=5000`; the Web CSV reports `100.0%` / bit `828.419` at rank `1`. The blocker is therefore obtaining a same-snapshot Web CSV/full-DB oracle and resolving top-N tie behavior, not changing `searchsp`. |

Recommended execution order:

1. Decide how strict Web-vs-local ordering should be for hits tied near the
  `max_target_seqs=500` boundary. The first 16S mismatch is rank 111, while the
  top hit and database statistics match.
2. Add a comparator mode or separate report for set-overlap / top-N tie windows
  before using Web XML as the sharded merge oracle.
3. Run the same comparator against `core_nt` hit-positive evidence after the
  small-database path is proven.
4. Only promote a database default in `api/services/web_blast_searchsp.py` after
  the Web XML, local full DB XML, sharded merged XML, comparator JSON, and run
  logs are all saved under `docs/temp/`.

### Runtime Equivalence Contract

The dashboard's default warmed-database submit path is now **precise sharded
BLAST**, not approximate sharding. A result may be described as NCBI Web
BLAST-compatible only when all of the following are true:

1. The selected database is warmed on AKS node-local storage and has prepared
  shard layouts for the selected workload node count.
2. The submit uses `sharding_mode=precise`; approximate sharding is a throughput
  probe mode and does not carry the Web-equivalence claim.
3. The BLAST options include a verified full-database effective search space
  (`-searchsp`) for the exact database snapshot and option scope, either from
  `api/services/web_blast_searchsp.py`, storage metadata, or an explicit caller
  override.
4. The output format is merge-supported (`outfmt 5` XML or `outfmt 6` /
  `outfmt '6 std ...'`) and the merge/finalizer report is kept with the run.
5. Evidence includes a canonical comparator report. Strict equality remains the
  pass condition for final claims; tie-window equivalence is diagnostic evidence
  for top-N boundary investigations, not a final replacement for strict order.

The frontend therefore prefers the `Web-equivalent shard` mode whenever a warmed
prepared database fits the selected cluster. The backend pre-flight and submit
gates continue to block precise sharding when the search-space or query metadata
needed for full-DB statistics is missing.

### 2026-05-18 Runtime Checkpoint

Current deployed/local-control-plane observation:

- AKS `elb-cluster` in `rg-elb-01` is `Succeeded` / `Running` with a 1-node
  `systempool` and a 10-node `blastpool` (`Standard_E16s_v5`).
- `core_nt` node-local warmup is `Ready` on `10/10` shards (`00` through `09`),
  with no active or failed warmup pods reported by `/api/monitor/aks/warmup-status`.
- The worker's scheduled `reconcile_auto_warmup` task returns `already_ready`,
  so no remedial warmup action is needed at this checkpoint.
- A current precise sharded submit payload passes `/api/blast/pre-flight` with
  `ready: true`, `critical_blockers: 0`, and `sharding_precision` =
  `precise_single_query`. The route injects the verified `core_nt` Web BLAST
  search space when the caller has not supplied an explicit `-searchsp`.

Comparator status:

- Existing no-hit `core_nt` calibration evidence remains strictly equivalent:
  `docs/temp/core-nt-searchsp/fresh-2026-05-17/live-finalizer-5be97da5/canonical-compare.json`
  reports `equivalent: true` and `difference_count: 0`.
- Current F3L positive-hit Web XML vs sharded `core_nt` evidence is **not**
  equivalent: `docs/temp/f3l-core-nt-2026-05-17/current-web-xml-vs-webmask-2026-05-18.json`
  reports `shared_accessions: 1`, `web_only: 499`, `candidate_only: 499`,
  `top10_overlap: 0`, and `tie_window_equivalent: false`.
- Older F3L Web CSV exports also remain non-equivalent against the current
  sharded candidate. The inclusive CSV has `shared_accessions: 0/500`; the
  exclusive CSV has `shared_accessions: 0/329`. This confirms the blocker is
  same-snapshot/top-N candidate selection, not only XML serialization or CSV
  parsing.
- The Web top-500 accessions are all present in the wider local candidate pool
  (`500/500` overlap across 11,261 candidates), but only `1/500` survives the
  current top-500 merge. The immediate optimization target is therefore the
  tie/order selection at the `max_target_seqs` boundary, ideally by preserving
  the original BLAST database subject order rather than guessing from accession
  strings.
- Running the XML/outfmt6 comparator against that wider pool confirms the same
  diagnosis: `current-web-xml-vs-widepool-2026-05-18.json` reports
  `shared_accessions: 500`, `web_only: 0`, `value_mismatch_count: 0`, and
  `tie_window_equivalent: true`. This is diagnostic only; the strict final
  top-500 output is still non-equivalent until the merge selects the same 500
  tied hits in the same order.

Next optimization target: keep precise sharding as the default, then improve the
positive-hit merge/oracle path until Web XML, Web CSV, local full DB, and
sharded merged output all agree under the strict comparator. Tie-window reports
should be used to diagnose boundary behavior, but the final claim still requires
strict equality.

The sharded finalizer now records `tie_cutoff_overflow_count` and
`tie_cutoff_queries` in `merge-report.json` whenever `max_target_seqs` cuts
through a tied score class. This does not change result ordering yet; it makes
the Web-equivalence blocker observable in every production run so the next
optimization can target only affected queries.

On the current F3L/core_nt wide-pool evidence, rerunning the merge helper with
the new diagnostic reports `total_input_hits: 11261`, `total_output_hits: 500`,
`tie_break_count: 11085`, and `tie_cutoff_overflow_count: 8620`. The cutoff
score class alone has `9120` tied hits, of which only `500` can be selected for
`max_target_seqs=500`. This explains why a biologically identical candidate pool
still fails strict Web top-500 equality without a Web-compatible subject-order
tie breaker.

### 2026-05-18 Tie-Order Oracle Result

An expanded offline inference pass now records its inputs and candidate scores
with `scripts/dev/infer-blast-tie-order.py`:

- `tie-order-inference-2026-05-18.json`
- `tie-order-inference-2026-05-18.md`

The pass evaluated `249` deterministic keys over accession, accession number,
version, title, year, local OID, GI, sequence length, shard id, volume/OID,
subject coordinates, prefix distributions, hash values, and two-key metadata
combinations. The best synthetic key was still weak (`year_desc`,
`top500_overlap: 33`, `top100_overlap: 3`, `top10_overlap: 0`,
`same_top: false`). This confirms again that the current evidence does not
support a safe fabricated Web BLAST tie-breaker.

The productive path is an explicit same-snapshot order oracle. The sharded merge
helper now accepts `ELB_TIE_ORDER_FILE`, a newline/TSV/outfmt6 accession order
file. When present, ties are sorted by `(primary BLAST score, oracle rank,
original ordinal)`. For a top-N membership oracle, `ELB_TIE_ORDER_STRICT=1`
also excludes non-oracle hits before truncation. The ElasticBLAST finalizer
patch looks for `${ELB_RESULTS}/${ELB_METADATA_DIR}/tie-order-oracle.txt`; if
that blob exists, it downloads it, exports `ELB_TIE_ORDER_FILE`, and enables
strict oracle mode by default.
The submit API can now carry `tie_order_oracle_text` or
`tie_order_oracle_accessions`; the worker uploads it to
`results/<job>/metadata/tie-order-oracle.txt` before invoking ElasticBLAST.

Using the current Web top-500 accession list as that oracle against the wide
candidate pool produces strict equality:

| Comparator field | Result |
| --- | ---: |
| `equivalent` | `true` |
| `exact_order` | `true` |
| `shared_accessions` | `500` |
| `web_only` | `0` |
| `candidate_only` | `0` |
| `value_mismatch_count` | `0` |
| `top10_overlap` | `10` |
| `top100_overlap` | `100` |

With strict mode enabled on the same evidence, the finalizer excludes all
non-oracle candidates before top-N selection. The strict F3L run reports
`total_input_hits: 11261`, `total_output_hits: 500`, `tie_break_count: 499`,
`tie_cutoff_overflow_count: 0`, and the comparator still reports
`equivalent: true`, `exact_order: true`, and `value_mismatch_count: 0`.

A second same-snapshot test used the local 16S full-run XML order as the oracle
for the existing contiguous sharded XML artifacts. Non-strict oracle sorting
matched the first 110 accessions, then admitted a non-oracle higher-score shard
hit (`NR_119263`) that the full BLAST run did not report. Strict oracle mode
fixed membership and order: the strict remerge produced `500` hits with
`exact_accession_order: true`, `tie_cutoff_overflow_count: 0`, and
`tie_break_count: 343`. The remaining canonical XML comparator differences are
surface/provenance fields from the synthetic FASTA shard DB regeneration:
`500` `Hit_id` values lack the `gi|...|` prefix and `5` `Hit_def` values differ.

This proves the finalizer can produce a Web/full-run-identical top-N membership
and order when the missing same-snapshot order is supplied. It does not prove
that a Web order can be synthesized from local shard metadata; the inference
evidence says the opposite. The next correctness task is therefore oracle
production: either run a same-snapshot full local BLAST order probe
before/alongside the sharded run, or obtain the Web/NCBI filtered subject order
for that exact database snapshot.

### 2026-05-17 hit-positive Web RID evidence

Evidence directory:
`docs/temp/web-blast-equivalence/2026-05-17-16s-carnobacterium/`.

| Field | Value |
| --- | ---: |
| Query | `scripts/dev/test_queries/16S_carnobacterium.fa` |
| Query length | `1486` nt |
| Web database value | `rRNA_typestrains/16S_ribosomal_RNA` |
| Local database basename | `16S_ribosomal_RNA` |
| RID | `0JXX09HH016` |
| Program | `blastn` / megablast |
| Options | `WORD_SIZE=28`, `EXPECT=10`, `HITLIST_SIZE=500`, `FILTER=L` |
| Web hit count | `500` |
| Web HSP count | `500` |
| Web `Statistics_db-len` | `40051470` |
| Web `Statistics_db-num` | `27648` |
| Top hit | `gi\|219857622\|ref\|NR_025211.1\|` |

The earlier RID `0JXTP4A0014` used `DATABASE=16S_ribosomal_RNA` directly. It is
kept in the same evidence directory as a negative control showing why Web DB ids
must be captured from the Web form rather than inferred from local BLAST DB
basenames.

### 2026-05-17 local 16S baseline evidence

Local BLAST+ 2.17.0 was installed under `~/.local/elb-tools/ncbi-blast-2.17.0+`
and the 16S database was downloaded under
`~/.cache/elb-dashboard/blastdb/16S_ribosomal_RNA/`. This avoids AKS pod image
pull/scheduling overhead for small Web-equivalence probes.

Evidence files in
`docs/temp/web-blast-equivalence/2026-05-17-16s-carnobacterium/`:

- `local-update-blastdb.log`
- `local-blastdbcmd-info.txt`
- `local-blastn-version.txt`
- `local-full-16s-ribosomal-rna.xml`
- `local-full-16s-summary.json`
- `web-vs-local-16s-canonical-compare.json`
- `web-vs-local-16s-summary.json`

Local baseline results:

| Field | Value |
| --- | ---: |
| Runtime | `2.54s` |
| Local hit count | `500` |
| Local HSP count | `500` |
| Local `Statistics_db-len` | `40051470` |
| Local `Statistics_db-num` | `27648` |
| Local `Statistics_eff-space` | `57425628120` |
| Same top hit as Web | `true` |
| First Web/local hit-order mismatch | rank `111` |
| Web/local hit ID overlap | `438 / 500` |

The strict canonical comparator reports `difference_count=5991`, so this is not
yet a passing Web-vs-local equivalence case. The useful discovery is narrower:
the database snapshot and top hit are aligned, but Web XML uses `eff_space=0` for
this 16S result and hit ordering diverges in the lower-ranked tied region.

### 2026-05-17 local 16S sharded positive probe

To avoid AKS scheduling overhead while testing merge behavior, the local 16S DB
was split into four synthetic shards and searched with the same query. Each shard
run used the full local search space (`-searchsp 57425628120`) before merging.

Evidence directories:

- `docs/temp/web-blast-equivalence/2026-05-17-16s-carnobacterium/local-sharded-16s/`
- `docs/temp/web-blast-equivalence/2026-05-17-16s-carnobacterium/local-sharded-16s-contiguous/`

Fast-path timings:

| Step | Time |
| --- | ---: |
| Extract local 16S FASTA | `1.81s` |
| Round-robin shard BLAST | `1.03s` to `1.06s` per shard |
| Contiguous shard BLAST | `0.93s` to `1.01s` per shard |

Both sharded probes produced merged XML with the same merged statistics as the
local full baseline:

| Field | Full local | Merged sharded |
| --- | ---: | ---: |
| `Statistics_db-len` | `40051470` | `40051470` |
| `Statistics_db-num` | `27648` | `27648` |
| `Statistics_eff-space` | `57425628120` | `57425628120` |
| `Statistics_hsp-len` | `26` | `26` |

Strict hit ordering is not yet equivalent. With contiguous shards, the same top
accession and top-10 order are preserved, but the first accession mismatch is at
rank 15 (`NR_036793` in the full run vs `NR_041841` in the merged sharded run).
The top-500 accession overlap is `442 / 500`. Testing tie-break keys based on
accession, identity/gaps, and original FASTA global ordinal did not reproduce the
full BLAST internal ordering. This confirms that the next productive work is not
more infrastructure setup; it is defining or implementing a BLAST-compatible
tie-break strategy for hit-positive sharded merge results.

The critical database-version check is positive for this 16S probe. The NCBI Web
XML does not expose a database release date, but it reports the same database
cardinality as the local May 14 2026 BLAST database: `Statistics_db-len=40051470`
and `Statistics_db-num=27648`. The same local database was then used for both the
full baseline and the synthetic sharded run. Evidence is saved in
`db-snapshot-and-value-equivalence.json`:

| Comparison | Shared accessions | Shared accessions with identical primary HSP/value fields | Notes |
| --- | ---: | ---: | --- |
| Web vs local full | `438` | `438` | Same DB size; shared hits have identical primary HSP values and hit fields. |
| Local full vs local sharded | `442` | `437` | Same DB size and statistics; five shared hits differ only in `Hit_def` GI formatting after FASTA-based shard DB regeneration. |

This means the remaining blocker is not a different 16S DB snapshot for the
shared hits. It is top-N membership/order in tied or near-tied hit regions, plus
Web XML's special `Statistics_eff-space=0` / `Statistics_hsp-len=0` reporting for
this Web 16S result.

### 2026-05-17 MPXV F3L Web CSV evidence

Two NCBI Web BLAST CSV exports were added under `docs/temp/` for the Monkeypox
F3L query `NC_063383.1:c46483-46022`:

- `blast_inclusive_F3L_928998.csv` (`500` Web rows)
- `blast_exclusive_F3L_928998.csv` (`329` Web rows)

The query sequence is already present as
`scripts/dev/test_queries/MPXV_F3L.fa`. A new dev comparator,
`scripts/dev/compare-blast-web-csv.py`, treats these CSV exports as the Web
reference and compares them against BLAST outfmt 6 sharded output by accession,
order, and value fields (`identity_pct`, `evalue`, `bits`, coordinates,
alignment length, and gaps).

The only matching sharded F3L output currently present is the older sibling v3
benchmark file
`~/dev/elastic-blast-azure/benchmark/results/v3/raw/B1-S10/merged_all.out`. That
run is not the same DB snapshot/source as the new Web CSV exports:

| Web CSV | Web rows | Candidate rows | Shared accessions | Top-10 overlap | First Web hit | First candidate hit |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `blast_inclusive_F3L_928998.csv` | `500` | `500` | `0` | `0` | `OZ461628.1` | `AY603973.1` |
| `blast_exclusive_F3L_928998.csv` | `329` | `500` | `0` | `0` | `OM460002.1` | `AY603973.1` |

Evidence reports:

- `docs/temp/f3l-web-inclusive-vs-v3-sharded-summary.json`
- `docs/temp/f3l-web-exclusive-vs-v3-sharded-summary.json`

This is useful negative evidence: the current Web CSV oracle cannot validate the
existing v3 sharded benchmark because the accession universe has changed. The
next valid pass requires a fresh sharded run against the same NCBI Web BLAST DB
snapshot and the same taxonomy inclusion/exclusion settings used for these CSV
exports.

A fresh AKS `core_nt` local-SSD run was then executed with the same MPXV F3L
query and inclusive taxonomy setting (`taxid=10244`) to test the current sharded
database rather than the older sibling benchmark.

Fresh run evidence:

| Field | Value |
| --- | --- |
| API job id | `23e0707e-2c25-4ec5-8b3a-ac51f666e2a4` |
| Celery task id | `0cf04700-f7a3-4c55-9b6f-ab2bd96ab15b` |
| ElasticBLAST job id | `job-0559818ee6894f1eb48b0ffa81995724` |
| Kubernetes suffix | `81995724` |
| Query | `scripts/dev/test_queries/MPXV_F3L.fa` (`462` nt) |
| Database | `core_nt`, source version `2026-05-09-01-05-02` |
| Search space | `-searchsp 32156241807668` |
| Sharding | `10` local-SSD shards, `Standard_E16s_v5`, prefix `https://elbstg01.blob.core.windows.net/blast-db/10shards/core_nt_shard_` |
| Init runtime | `RUNTIME init-sharded-storage 294.957081658009 seconds` |
| BLAST shard runtimes | `18s` to `19s` for `blastn-batch-s00..s09-job-000-81995724` |
| Finalizer | `elb-finalizer-81995724`, `Complete`, `46s` |
| Result blobs | `merged_results.out.gz`, `merge-report.json`, `metadata/SUCCESS.txt` |
| Local evidence | `docs/temp/f3l-core-nt-2026-05-17/` |

The run produced `500` candidate outfmt 6 rows and completed successfully, but
it still does not match the provided Web CSV export:

| Web CSV | Web rows | Candidate rows | Shared accessions | Top-10 overlap | First Web hit | First candidate hit |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `blast_inclusive_F3L_928998.csv` | `500` | `500` | `0` | `0` | `OZ461628.1` | `PZ276635.1` |

Evidence files:

- `docs/temp/f3l-core-nt-2026-05-17/inclusive-submit-payload-taxdb-patched.json`
- `docs/temp/f3l-core-nt-2026-05-17/inclusive-submit-response-taxdb-patched.json`
- `docs/temp/f3l-core-nt-2026-05-17/results/merged_results-taxdb-patched.out.gz`
- `docs/temp/f3l-core-nt-2026-05-17/results/merged_results-taxdb-patched.outfmt6`
- `docs/temp/f3l-core-nt-2026-05-17/inclusive-web-vs-sharded-taxdb-patched-summary.json`
- `docs/temp/f3l-core-nt-2026-05-17/recheck-inclusive-web-vs-sharded-summary.json`
- `docs/temp/f3l-core-nt-2026-05-17/recheck-shards/shard_00..09.outfmt6`

Recheck on 2026-05-17 corrected an earlier too-narrow shard-00-only check. The
Web CSV top accessions are present in the current sharded database, but not with
the same alignment as the Web CSV export. `blastdbcmd` across all 10 local-SSD
shards found the Web top accessions in shard 05 and the fresh candidate top
accessions in shard 00:

```text
SHARD 00
PZ276635.1 10244 Monkeypox virus
OQ511287.1 10244 Monkeypox virus

SHARD 05
OZ461628.1 10244 Monkeypox virus
OZ254846.1 10244 Monkeypox virus
```

The same shard 05 was then searched directly with the MPXV F3L query and a wider
`-max_target_seqs 5000` cap. The Web top hits are in the tied/near-tied pool, but
their current-DB HSP values do not match the Web CSV:

```text
1013 NC_063383.1:c46483-46022  OZ461628.1  98.701  462  6  0  1  462  46970  46509  0.0  821
1090 NC_063383.1:c46483-46022  OZ254846.1  98.701  462  6  0  1  462  48309  47848  0.0  821
```

The Web CSV reports those same accessions at rank 1 and 2 with `100.0%`, `462`
identities, `0` gaps, and bit score `828.419`. That makes the fresh comparison a
snapshot/content mismatch for these accessions, with a separate top-N tie
selection issue because many current-DB exact hits score above them.

Taxonomy filtering also exposed a runtime cache footgun in the sibling
`elastic-blast-azure` repo. The sharded local-SSD init script already included
`taxdb.btd` and `taxdb.bti` in the download pattern, but a warm-cache marker could
skip the taxdb refresh when the shard files were already present. The local
sibling script `src/elastic_blast/templates/scripts/init-db-shard-aks.sh` was
patched so cache validation treats missing `taxdb.btd` / `taxdb.bti` as an
incomplete cache and can repair the taxdb files before writing
`/tmp/shard_volpaths.txt`. Direct pod verification on node ordinal `0` showed the
BLAST runtime mount contains `taxdb.btd`, `taxdb.bti`, `core_nt.ndb`,
`core_nt.ntf`, and `core_nt.nto` alongside `core_nt_shard_00.nal`.

Follow-up fast probes showed the warning is meaningful for BLAST+ 2.17.0
taxonomy expansion. `taxdb.btd` and `taxdb.bti` are not sufficient on their own;
`taxonomy4blast.sqlite3` from NCBI `taxdb.tar.gz` must also be present on the
BLASTDB search path. With only `taxdb.btd/bti`, `blastn -taxids 10244` returns
hits but prints the additional-data warning. After adding `taxonomy4blast.sqlite3`
in `/tmp/taxdb` and adding that directory to `BLASTDB`, the warning disappears
and a negative-control search with `-taxids 9606` returns `0` rows, proving the
taxid filter is active. The dashboard warmup script and sibling local-SSD shard
init script now include `taxonomy4blast.sqlite3` in their download/cache checks.

The current Web BLAST XML oracle for the same MPXV F3L query is RID
`0K7GE593016`, saved at
`docs/temp/f3l-core-nt-2026-05-17/current-web-0k7ge593016.xml`. It used
`PROGRAM=blastn`, `core_nt`, `MEGABLAST=on`, `EXPECT=0.05`, `WORD_SIZE=28`,
`HITLIST_SIZE=500`, `FILTER=L`, and
`ENTREZ_QUERY=txid10244[Organism:exp]`. The first Web HSP is a 462/462 perfect
identity alignment but reports raw score `448` and bit score `828.419`. A cached
shard score matrix reproduced that only with:

```text
-dust yes -soft_masking false
```

Plain `-dust yes`, `-dust no`, and `-soft_masking true` all reported raw score
`462` and bit score `854.272` for the same perfect alignment. `dustmasker` masks
query interval `210 - 216`, and hard masking accounts for the `14` raw-score
difference on the reverse-complement HSP. The generated dashboard config now
adds `-soft_masking false` for Web-compatible `blastn` low-complexity filtering
unless the caller explicitly supplies `-soft_masking`.

The latest fast all-shard probes avoid a full ElasticBLAST rerun. They launch one
short pod per warmed node, build a temporary alias from that node's local
`core_nt.*.nsq` volumes, inject `taxonomy4blast.sqlite3`, run BLAST with Web-like
masking, and merge the top 500 rows for comparison with Web XML. Evidence lives
under:

- `docs/temp/f3l-core-nt-2026-05-17/fast-taxonomy4blast-check.log`
- `docs/temp/f3l-core-nt-2026-05-17/fast-taxid-negative-control.log`
- `docs/temp/f3l-core-nt-2026-05-17/fast-mask-score-matrix.log`
- `docs/temp/f3l-core-nt-2026-05-17/all-shards-webmask-full/current-web-vs-merged-top500.webmask.json`
- `docs/temp/f3l-core-nt-2026-05-17/all-shards-webmask-full/current-web-vs-merged-top500.webmask.with-score.json`

The Web-like masking probe now matches the Web raw-score class, but strict top-N
equivalence still fails: shared accessions are `1 / 500`, top-10 overlap is `0`,
and both Web and local candidates contain hundreds of 462/462 perfect matches at
the same score. When the candidate rows include raw score as an optional 13th
outfmt 6 column, `value_mismatch_count` drops to `0`; the previous bit-score
difference was only outfmt 6 display precision (`828` vs Web XML `828.419`). At
that point the remaining hypotheses were tied-hit subset/order and database
snapshot/subset differences, not `searchsp`, DUST score, or missing taxonomy
files.

Follow-up on 2026-05-17 separated the two remaining hypotheses. A wider cached
local-SSD probe extracted all 500 Web XML accessions, checked them against the
current warmed `core_nt` shard volumes with `blastdbcmd`, searched those exact
accessions with Web-style hard masking, and also built a deduplicated local
candidate pool from 10 short node-local probes.

Evidence directory:
`docs/temp/f3l-core-nt-2026-05-17/web-top500-local-status/`.

Key files:

- `web-top500-accessions.txt`
- `web-top500-values.tsv`
- `webgap-probe-00.log` through `webgap-probe-09.log`
- `web-top500-local-status.json`
- `merged-wide-webmask-dedup.outfmt6.tsv`
- `current-web-vs-merged-wide-webmask.tie-window.json`

Result:

| Question | Answer |
| --- | ---: |
| Web XML rows | `500` |
| Web accessions present in current cached DB | `500 / 500` |
| Web accessions returned by targeted local BLAST | `500 / 500` |
| Web accessions present in the deduplicated wide local pool | `500 / 500` |
| Web accessions with identical primary HSP/value fields | `500 / 500` |
| Deduplicated wide local pool size | `11,261` accessions |
| Web overlap with local wide top 500 | `32 / 500` |
| Web overlap with local wide top 1,000 | `75 / 500` |
| Web overlap with local wide top 5,000 | `332 / 500` |

The wide local pool contains `9,118` unique accessions in the same top score
class: `100.000%` identity, length `462`, mismatches `0`, gaps `0`, e-value `0`,
raw score `448`, displayed bit score `828`. The Web top 500 is entirely inside
that same score class and every Web accession has the same primary local HSP
values. The strict order still differs (`top10_overlap = 0`, `top100_overlap =
1` in strict rank comparison), but the new comparator report sets
`tie_window_equivalent = true` for the wide candidate pool.

**Important interpretation: biological equivalence is not strict rank identity.**

For this MPXV F3L oracle, "biologically equivalent" means the Web BLAST hits are
present in the current local `core_nt` database and have the same primary HSP
evidence: same accession availability, same 462 nt perfect alignment, same query
and subject coordinates, same mismatch/gap counts, same e-value, same bit score,
and same raw score. In other words, the scientific BLAST statement is the same:
the query has thousands of indistinguishable 462/462 perfect matches in the
filtered MPXV/core_nt hit set.

It does **not** mean the first 500 displayed rows, or their order, are identical
to NCBI Web BLAST. Web BLAST chooses 500 rows from a tied score window containing
thousands of equally scoring hits. The local sharded pipeline can reproduce the
hit evidence and can report tie-window equivalence, but exact Web top-500
membership/order requires the Web-side tied-hit truncation order (or a matching
same-snapshot full-run order) as an additional oracle.

This closes the DB-snapshot/subset question for the current MPXV F3L Web XML
oracle: the Web top-500 accessions are not absent from the current cached DB and
do not have different HSP values. The remaining difference is top-N membership
and ordering inside a very large tied score window. For user-facing equivalence
claims, strict accession order should remain a separate mode from biological
tie-window equivalence.

An additional tie-break search tested whether the Web top-500 order can be
reconstructed from metadata available in the current sharded BLAST DB. The probe
extracted `blastdbcmd` metadata for the perfect-hit pool (`%a`, `%o`, `%g`, `%l`,
`%T`, `%t`) and scored candidate orderings against the Web XML order.

Additional evidence files:

- `perfect-hit-metadata.tsv`
- `perfect-hit-volume-oids.tsv`
- `tie-break-key-score-report.json`
- `tie-break-merge-strategy-score-report.json`
- `tie-break-volume-oid-report.json`
- `tie-break-hash-score-report.json`

Result: simple public/local keys do not explain Web ordering. The best tested
metadata key (`year_desc_oid_asc`) reached only `33 / 500` Web overlap in its
top 500. DB OID ascending/descending, GI ascending/descending, length, title,
year, shard-local BLAST output order, concatenating shard output, and
round-robin merging shard output all stayed in the same low-overlap range
(`0..36 / 500`). A short NCBI E-utilities check showed the public Entrez
`txid10244[Organism:exp]` relevance/date order starts with the latest `PZ*`
records, not the Web BLAST top accession `PX485240.1`, so the Web strict order
is not reproduced by the default public Entrez sort either.

Follow-up probes also ruled out the most plausible hidden local keys:

- Direct per-volume `blastdbcmd` lookup recovered accession -> `core_nt.NN` +
  volume-local OID for all `9,280` metadata rows, including all `500 / 500` Web
  accessions. Sorting perfect-hit candidates by volume/OID or coordinate +
  volume/OID still peaked at `36 / 500` Web top-500 overlap.
- The Web XML contains exactly one HSP per top-500 hit, and every hit has the
  same total raw score (`448`) and bit score (`828.419`). Hidden multi-HSP total
  score is therefore not the missing tie-breaker for this oracle.
- Hash-like orderings over accession/title strings (`crc32`, `adler32`, `md5`,
  `sha1`, `sha256`, plus bucketed hash variants) stayed random-like; the best
  tested hash candidate reached only `40 / 500` Web top-500 overlap.
- The Web top-500 rank sequence jumps across volumes and shards almost every
  row. In the first 100 Web ranks, shard runs are overwhelmingly length 1, and
  the top-500 volume distribution is broad rather than contiguous. This does not
  resemble volume scan order, shard concatenation, or a simple round-robin merge.
- `blastdbcmd` cannot emit `-entry all` together with `-taxids`, so the taxid
  subset iteration order cannot be obtained through that CLI. The warmed node's
  `/blast/blastdb` did not expose a readable `taxonomy4blast.sqlite3` posting
  table; the current local evidence path exposes taxid membership, not the
  Web-side posting-list order used during tied hit truncation.

This means strict Web top-N list/order matching needs one of these additional
inputs:

1. A same-snapshot full local BLAST run whose output order matches Web, so the
  sharded finalizer can learn and preserve the full-run tie order.
2. A reliable Web/NCBI internal tie-order oracle for the filtered `core_nt`
  subset, such as the exact database ordinal/posting-list order used by Web
  BLAST after `ENTREZ_QUERY` filtering.
3. A product decision to report strict accession order separately from
  tie-window equivalence when thousands of hits share identical HSP scores.

Without one of those, a deterministic sharded merge can be stable and
biologically equivalent, but it cannot honestly claim byte-for-byte or
rank-for-rank Web top-500 identity for this query.

Performance note: the latest warmed `core_nt` shard jobs completed in `17s` to
`19s`, so the remaining equivalence work should focus on hit-positive golden
coverage and comparator strictness. The live finalizer currently took `46s` for
10 small shard XML files because shard downloads are serialized; if end-to-end
latency must stay comfortably below one minute, parallelizing finalizer shard
downloads is the next optimization.

### Resource Shape

Use a temporary resource group dedicated to the experiment. The safest default is
to delete that whole resource group after validation, because it removes the VM,
OS disk, data disk, NIC, public IP, NSG, and generated VNet together.

Recommended starting point:

| Resource | Recommendation | Reason |
| --- | --- | --- |
| VM size | `Standard_E96s_v5` if quota allows, otherwise `Standard_E64s_v5` or larger | `core_nt` is roughly 280 GB before local working overhead; 512 GB+ RAM leaves room for BLAST database indexes and OS cache. |
| OS image | Ubuntu 22.04 LTS | Matches the Linux BLAST+ tooling path used by the terminal sidecar. |
| Data disk | 1 TiB Premium SSD minimum, 2 TiB preferred for repeated probes | Holds decompressed database volumes, query inputs, XML output, logs, and optional `-searchsp 1` comparison output. |
| Network | Temporary public IP restricted to the caller IP for SSH, or a VM in the platform VNet if copying from private Storage | Do not enable production Storage public access for this experiment. |
| Tooling | BLAST+ 2.17.0, `azcopy`, `jq`, `pigz`, `tmux`, Python 3 | Keeps the baseline aligned with current Web XML and terminal image observations. |

The helper script [scripts/dev/core-nt-searchsp-calibration.sh](../scripts/dev/core-nt-searchsp-calibration.sh)
prepares the temporary VM path with explicit approval gates. It prints the
VM-side commands instead of running disk formatting automatically:

```bash
# Prints the resolved configuration. No Azure resources are created.
scripts/dev/core-nt-searchsp-calibration.sh plan

# Creates the temporary resource group and VM only after explicit approval.
ELB_CORE_NT_CREATE_APPROVED=1 \
  scripts/dev/core-nt-searchsp-calibration.sh create \
  --rg rg-elb-core-nt-searchsp-20260516 \
  --location eastus \
  --vm-size Standard_E96s_v5

# Prints VM-side commands for mounting the data disk, installing BLAST+ 2.17.0,
# downloading core_nt, running the baseline, and packaging results.
scripts/dev/core-nt-searchsp-calibration.sh vm-runbook \
  --rg rg-elb-core-nt-searchsp-20260516

# Runs that VM-side calibration script over SSH. It formats only the throwaway
# data disk and requires an explicit remote approval gate.
ELB_CORE_NT_REMOTE_APPROVED=1 \
CORE_NT_DOWNLOAD_JOBS=6 \
CORE_NT_SPLIT_CONN=4 \
  scripts/dev/core-nt-searchsp-calibration.sh remote-calibrate \
  --rg rg-elb-core-nt-searchsp-20260516

# Copies the result archive back before cleanup.
scripts/dev/core-nt-searchsp-calibration.sh fetch-results \
  --rg rg-elb-core-nt-searchsp-20260516

# Deletes the entire temporary resource group only when both confirmations match.
ELB_CORE_NT_DELETE=delete-rg-elb-core-nt-searchsp-20260516 \
  scripts/dev/core-nt-searchsp-calibration.sh delete \
  --rg rg-elb-core-nt-searchsp-20260516 \
  --confirm-resource-group rg-elb-core-nt-searchsp-20260516
```

Do not run `create` until the VM size, region, quota, and budget have been
approved. Do not leave the VM running after the XML, metadata, and `searchsp`
evidence have been copied out.

### Database Preparation

For the first calibration, prefer downloading `core_nt` directly from NCBI on the
temporary VM. This avoids changing the network posture of the workload Storage
account. A separate Azure Storage copy path is acceptable only if the VM is placed
inside the same private-network path as the Storage private endpoint, or if a
separate non-production storage source is explicitly prepared.

After printing `vm-runbook`, SSH to the VM and prepare the database on the mounted
data disk. The helper uses parallel `curl --continue-at -` workers to download the
`core_nt.*.tar.gz` volumes; tune `CORE_NT_DOWNLOAD_JOBS` for the number of files
fetched at once. Start with `6`; higher values may trigger NCBI HTTP 503s:

```bash
mkdir -p /mnt/blast/db /mnt/blast/input /mnt/blast/results /mnt/blast/metadata
cd /mnt/blast/db
curl -fsSL https://ftp.ncbi.nlm.nih.gov/blast/db/ \
  | grep -o 'core_nt[^"<]*\.tar\.gz' \
  | sort -u \
  | sed 's#^#https://ftp.ncbi.nlm.nih.gov/blast/db/#' \
  > /mnt/blast/metadata/core_nt-download-urls.txt
xargs -n 1 -P 6 bash -c '\
  url="$1"; file="${url##*/}"; \
  curl --fail --location --continue-at - --retry 30 --retry-all-errors \
    --retry-delay 10 --output "/mnt/blast/db/$file" "$url"\
' _ < /mnt/blast/metadata/core_nt-download-urls.txt \
  2>&1 | tee /mnt/blast/metadata/core_nt-download.log
find /mnt/blast/db -maxdepth 1 -name 'core_nt*.tar.gz' -print0 \
  | xargs -0 -n1 -P "$(nproc)" tar -xzf
blastdbcmd -db /mnt/blast/db/core_nt -dbtype nucl -info \
  | tee /mnt/blast/metadata/blastdbcmd-core_nt-info.txt
blastn -version | tee /mnt/blast/metadata/blastn-version.txt
```

If the database already exists in Azure Storage, keep the production rule intact:
do not issue SAS tokens to the browser and do not enable public access on the
workload account for this experiment. Use a VM identity and private network path,
or copy from a separate approved staging account.

### Full Database Baseline Run

Use the exact query FASTA and BLAST options expected for the sharded run. The
manual example below mirrors the external API defaults: `blastn`, `-word_size 28`,
`-dust yes`, `-evalue 10`, `-max_target_seqs 500`, and XML `-outfmt 5`.

```bash
export ELB_BLAST_OPTIONS='-word_size 28 -dust yes -evalue 10 -max_target_seqs 500 -outfmt 5'
export ELB_QUERY=/mnt/blast/input/query.fa
export ELB_DB=/mnt/blast/db/core_nt
export ELB_THREADS=$(nproc)

sha256sum "$ELB_QUERY" | tee /mnt/blast/metadata/query.sha256
printf '%s\n' "$ELB_BLAST_OPTIONS" | tee /mnt/blast/metadata/blast-options.txt

blastn \
  -query "$ELB_QUERY" \
  -db "$ELB_DB" \
  $ELB_BLAST_OPTIONS \
  -num_threads "$ELB_THREADS" \
  -out /mnt/blast/results/core_nt-full-default.xml \
  2>&1 | tee /mnt/blast/metadata/core_nt-full-default.stderr
```

Parse the XML statistics immediately after the run:

```bash
python3 - <<'PY'
import json
import xml.etree.ElementTree as ET
from pathlib import Path

xml_path = Path('/mnt/blast/results/core_nt-full-default.xml')
root = ET.parse(xml_path).getroot()
rows = []
for index, iteration in enumerate(root.findall('.//Iteration'), start=1):
    stats = iteration.find('./Iteration_stat/Statistics')
    rows.append(
        {
            'iteration': index,
            'query_id': iteration.findtext('Iteration_query-ID'),
            'query_def': iteration.findtext('Iteration_query-def'),
            'query_len': iteration.findtext('Iteration_query-len'),
            'db_num': stats.findtext('Statistics_db-num') if stats is not None else None,
            'db_len': stats.findtext('Statistics_db-len') if stats is not None else None,
            'hsp_len': stats.findtext('Statistics_hsp-len') if stats is not None else None,
            'eff_space': stats.findtext('Statistics_eff-space') if stats is not None else None,
        }
    )
out = Path('/mnt/blast/metadata/core_nt-full-default-stats.json')
out.write_text(json.dumps(rows, indent=2, sort_keys=True))
print(json.dumps(rows, indent=2, sort_keys=True))
PY
```

If `Statistics_eff-space` is populated, that is the full-database reference for
the corresponding query/options/database snapshot. If the sharded execution uses
one BLAST invocation per query group, preserve the per-query or per-group mapping
rather than collapsing unrelated queries into one value.

### Fallback Inference Check

If the XML statistics are missing or suspicious, run a second full-database pass
with `-searchsp 1` and infer the default effective search space from stable hits:

```bash
blastn \
  -query "$ELB_QUERY" \
  -db "$ELB_DB" \
  $ELB_BLAST_OPTIONS \
  -searchsp 1 \
  -num_threads "$ELB_THREADS" \
  -out /mnt/blast/results/core_nt-full-searchsp-1.xml \
  2>&1 | tee /mnt/blast/metadata/core_nt-full-searchsp-1.stderr
```

For each matched hit in the default and `-searchsp 1` outputs:

```text
inferred_full_searchsp = default_evalue / searchsp_1_evalue
```

Use this only as a cross-check or fallback. Prefer the XML
`Statistics_eff-space` value when BLAST+ reports it for the full database.

### Sharded Comparison Rule

When running the same query/options against AKS shards, pass the calibrated full
database value to every shard:

```text
blastn ... -db <shard-db> -searchsp <full_db_eff_space> ...
```

Do not infer or use shard-local default search spaces. Shard-local values change
the statistical model and are expected to produce e-values that differ from the
full database run.

### Evidence to Save Before Cleanup

Copy these files to the project evidence location or an approved storage path
before deleting the temporary resource group:

- `core_nt-full-default.xml`
- `core_nt-full-default-stats.json`
- `core_nt-full-searchsp-1.xml`, if the fallback run was needed
- `blastdbcmd-core_nt-info.txt`
- `blastn-version.txt`
- `blast-options.txt`
- `query.sha256`
- VM size, Azure region, data disk SKU, run start/end timestamps, and wall-clock runtime

### Cleanup Checklist

Cleanup is part of the experiment, not an optional follow-up.

1. Confirm the metadata and XML evidence were copied out.
2. Stop any running BLAST process or `tmux` session on the VM.
3. Delete the temporary resource group, preferably through the guarded helper:

```bash
ELB_CORE_NT_DELETE=delete-rg-elb-core-nt-calibration \
  scripts/dev/core-nt-searchsp-calibration.sh delete \
  --rg rg-elb-core-nt-calibration \
  --confirm-resource-group rg-elb-core-nt-calibration
```

4. Confirm the group is gone:

```bash
az group exists --name rg-elb-core-nt-calibration
```

The command should return `false`. If a shared VNet or shared Storage path was
used instead of a dedicated temporary resource group, do not delete the shared
group; list and delete only the experiment-owned VM, OS disk, data disk, NIC,
public IP, and NSG resources after checking their tags.

## Raw Command Evidence

The validation run used:

```bash
python3 /tmp/ncbi_multi_searchsp.py > /tmp/ncbi_multi_searchsp.json
```

The repository version of the script can reproduce the same type of probe:

```bash
python3 scripts/dev/ncbi-searchsp-discovery.py --output /tmp/ncbi-searchsp-discovery.json
```

Because this script submits real NCBI Web BLAST jobs, it should be run manually
and sparingly, not as part of automated CI.