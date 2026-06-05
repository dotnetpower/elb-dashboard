# BLAST search pod: vmtouch warm step (restored where it actually works)

## Motivation

The 2026-06-06 prior change ("warmup-drop-fake-vmtouch") removed the
`/scripts/blast-vmtouch-aks.sh` step from the **warmup Job** entrypoint after
operator inspection of a live `elb-cluster-02` warmup pod proved it was a
1-second noop on already-cached pages with no mmap holder.

That change made the warmup Job's behaviour honest, but it also surfaced the
underlying gap: the upstream `src/elastic_blast/templates/scripts/blast-run-aks.sh`
in elastic-blast-azure (unlike NCBI's reference `splitq_download_db_search`)
**does not run vmtouch at all**. So even with a perfectly staged SSD, every
BLAST search pod was paying the full mmap-fault cost from cold SSD on the
first query — every shard on every node.

Restoring vmtouch **inside the BLAST search pod** is the variant where it
actually works: the `blastn` process that follows immediately holds an active
mmap reference to the same files, and the kernel deprioritises reclaim of
pages under an active mapping. The cache stays resident for the lifetime of
the search.

## User-facing change

A new `patch_blast_run_aks_script()` step in
[terminal/patch_elastic_blast.py](../../../terminal/patch_elastic_blast.py)
injects a vmtouch warm block into
`src/elastic_blast/templates/scripts/blast-run-aks.sh` (and any installed
package copy under `venv/lib/python*/site-packages/elastic_blast/...`)
immediately above the existing `start=$(date +%s); echo "run start ..."` block,
so it runs once per BLAST batch pod, right before the `blastn` invocation.

The injected block (literal shell text):

```sh
# ELB vmtouch warm step (added by patch_elastic_blast.py).
if [ "${ELB_VMTOUCH_DISABLE:-0}" != "1" ] && command -v vmtouch >/dev/null 2>&1; then
    if command -v blastdb_path >/dev/null 2>&1; then
        vm_start=$(date +%s)
        elb_vmtouch_awk='/MemAvailable/ {printf "%dG", int($2/1024/1024*0.6)}'
        ELB_VMTOUCH_MEM=${ELB_VMTOUCH_MEM:-$(awk "$elb_vmtouch_awk" /proc/meminfo)}
        echo "vmtouch warm: db=${ELB_DB} mol=${ELB_DB_MOL_TYPE} budget=${ELB_VMTOUCH_MEM}"
        blastdb_path -dbtype "$ELB_DB_MOL_TYPE" -db "$ELB_DB" -getvolumespath 2>/dev/null \
            | tr ' ' '\n' \
            | xargs -r -n1 vmtouch -tqm "$ELB_VMTOUCH_MEM" || true
        vm_end=$(date +%s)
        vm_db_label="vmtouch-${ELB_DB//\//-}"
        vm_runtime_line=$(printf 'RUNTIME %s %f seconds' "$vm_db_label" $((vm_end - vm_start)))
        echo "$vm_runtime_line"
        echo "$vm_runtime_line" >> "$BLAST_RUNTIME"
    fi
fi
```

Design choices, in order of risk:

* **`vmtouch -tqm` (touch + quiet + per-file memory cap), not `-l` or `-d`.** No
  mlock, no background daemon. The BLAST search pod's own mmap is the durable
  cache holder; using mlock here would require `CAP_IPC_LOCK` and locked
  memory ulimit changes on the Job spec, which is a wider change than this PR.
* **`-m` is a per-FILE cap, not a cumulative budget.** vmtouch's `-m` flag
  skips any single volume file larger than the cap; BLAST DB volumes are
  typically GB-scale per file so 60% of MemAvailable leaves any realistic
  volume well under the cap while still acting as a safety rail for a
  pathologically large single file. The override env var `ELB_VMTOUCH_MEM`
  lets a future run-profile tune this if shard volumes ever grow above a
  meaningful fraction of node RAM.
* **`|| true` after the `xargs vmtouch` pipeline.** Best-effort. A missing
  `vmtouch` binary on the node image, an empty volume list, or a partial DB
  staging must not abort the BLAST search itself — the search still benefits
  from the OS page cache that `azcopy` already populated as a download side
  effect.
* **`ELB_VMTOUCH_DISABLE=1` escape hatch.** Strict literal `"1"` check
  (anything else, including unset / `0` / `"true"` / empty, enables the
  step). Lets us disable the step per-pod via the Job env at runtime if a
  future BLAST search regression needs to bisect against the upstream
  behaviour without rebuilding the terminal image.
* **`vm_runtime_line` echoed to both stdout AND `$BLAST_RUNTIME`.** The
  existing `\time -o "$BLAST_RUNTIME" $ELB_BLAST_PROGRAM …` line that
  records the blastn runtime stays unchanged — our vmtouch line is
  appended above it. `results-export-aks.sh` already uploads
  `BLAST_RUNTIME-${JOB_NUM}.out` to Blob, so the SPA surfacing follow-up
  can read per-shard vmtouch timing from the same artefact path it
  already consumes for the BLAST runtime.
* **Idempotent re-patch.** Guarded by the literal `ELB vmtouch warm step`
  marker via the existing `_replace_once_unless_present` helper. Re-running
  `patch_elastic_blast.py` against an already-patched tree is a no-op,
  matching the contract of every other `patch_*` function in that file.

The `nodeAffinity` follow-up suggested in the prior change note was investigated
and is **already shipping**: the upstream
`blast-batch-job-shard-ssd-aks.yaml.template` already pins each batch Job
via `nodeSelector: { ordinal: "${ELB_SHARD_IDX}" }`, and elastic-blast's
own `_label_nodes()` in `src/elastic_blast/azure.py` attaches the matching
`ordinal=<N>` label to every node on every submit. No additional code
needed in this dashboard for that part.

## API/IaC diff summary

* [terminal/patch_elastic_blast.py](../../../terminal/patch_elastic_blast.py):
  * New `_BLAST_RUN_AKS_VMTOUCH_ANCHOR`, `_BLAST_RUN_AKS_VMTOUCH_BLOCK`,
    `_blast_run_aks_script_paths()`, and `patch_blast_run_aks_script()`.
  * `main()` calls `patch_blast_run_aks_script(root)` between
    `patch_init_shard_script` and `patch_aks_workload_tolerations`.
* [api/tests/test_terminal_patch_elastic_blast.py](../../../api/tests/test_terminal_patch_elastic_blast.py):
  * Four new tests covering the happy path (injection placement, vmtouch
    flags, escape hatch presence), idempotence, installed-package copy,
    and the missing-anchor failure mode.

No infra/Bicep change. No new container image flag.

## Validation evidence

* `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py` — 11/11
  passed (4 new + 7 pre-existing).
* `uv run pytest -q api/tests` — **2925 passed**, 3 skipped (parity tests
  gated on `ELB_PARITY_CANDIDATE_DIR`).
* `uv run ruff check api terminal` — clean.
* **Round-trip patch against the real upstream file**:
  `cp src/.../blast-run-aks.sh /tmp/orig` → run
  `patch_blast_run_aks_script(...)` → `bash -n` of the patched file →
  `SYNTAX OK` → restore. Confirms the inserted shell block parses cleanly
  in the exact upstream context.
* `uv run python scripts/docs/check_frontmatter.py` — OK
  (53 navigated pages; `features_change/**` is excluded from this guard by
  design but the parent docs tree is still green).
* `git diff --stat` —
  `api/services/warmup/scripts.py` +8 -2,
  `api/tests/test_warmup_jobs.py` +58 -1,
  `api/tests/test_terminal_patch_elastic_blast.py` +131,
  `terminal/patch_elastic_blast.py` +78. Only the four intended files dirty.

## Risks and rollback

* **Risk: vmtouch missing on the node image.** The patch guards with
  `command -v vmtouch`; if the binary is missing, the step silently no-ops
  and BLAST runs as before. The dashboard's terminal image already ships
  vmtouch (see [terminal/Dockerfile](../../../terminal/Dockerfile)), and the
  BLAST search pod uses the elastic-blast docker image which also bundles
  it via the elastic-blast Makefile. If a future image change removes it,
  the worst case is a return to today's behaviour.
* **Risk: 60% cap too aggressive on small-memory SKUs.** The escape hatches
  are `ELB_VMTOUCH_MEM` (override budget directly) and `ELB_VMTOUCH_DISABLE=1`
  (skip the step entirely) — both can be set on the BLAST Job env without
  rebuilding the terminal image.
* **Rollback.** This is a `terminal/patch_elastic_blast.py` change applied
  at terminal-image build time. Reverting the patch function (or shipping
  the previous terminal image) is sufficient to remove the vmtouch step
  from future search pods; running pods are unaffected.

## Follow-ups

* Double-nested `nodeSelector` in `blast-batch-job-shard-ssd-aks.yaml.template`
  (`workload: blast` is immediately overwritten by `ordinal: …`). Cosmetic for
  now because the YAML parser keeps the last occurrence (which is the one we
  want), but worth cleaning up in a focused PR via a new `_replace_once_…`
  call in `patch_aks_workload_tolerations()`.
* Surface per-shard `vmtouch-<db>` `RUNTIME` lines in the BLAST run timeline
  card in the SPA so operators can verify the warm step is doing real work
  (it should show 1-60 s on first run per shard, sub-1 s on subsequent
  re-runs while the shard is still resident).
