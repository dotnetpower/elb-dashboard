---
title: Warm node RAM on node_disk cluster restart (warmup DOWNLOAD_SKIP path)
description: On a node_disk/data_disk cluster restart the staged BLAST DB survives on the node disk so azcopy is skipped — but that skips the page-cache side effect the warmup relied on, leaving RAM cold and pushing the full disk→RAM cost onto the first search. The warmup Job entrypoint now vmtouches the shard into the page cache on the DOWNLOAD_SKIP path, off the first-search critical path.
tags:
  - operate
  - blast
---

# Warm node RAM on node_disk cluster restart

## Motivation

A review of "cluster start warmup takes a long time" on a `node_disk` cluster
traced the cost to an architectural gap in the warm-cache design.

The dashboard warmup Job's only RAM-warming mechanic is the **azcopy download
side effect**: `azcopy cp` writes the DB shard to the node disk and, as a
by-product, the OS write-back path leaves those pages in the node page cache.
The Job runs no explicit `vmtouch` (it was removed in
[2026-06-06-warmup-drop-fake-vmtouch.md](2026-06-06-warmup-drop-fake-vmtouch.md)
because on the download path it was a redundant noop).

On a **`node_disk`** cluster the staged DB and its `.download-complete` marker
survive an `az aks stop`/`start` deallocation on the Managed OS disk. The
warmup Job therefore hits `DOWNLOAD_SKIP` and skips azcopy entirely — which
also skips the page-cache side effect. The Job completes quickly and reports
`Ready`, **but node RAM is cold**. The full disk→RAM cost is then paid lazily
and serially by the **first BLAST search**, whose in-pod `vmtouch` (added by
`terminal/patch_elastic_blast.py`) reads the whole per-node shard from the OS
disk before `blastn` starts. That is the "slow warmup on start" the operator
sees: the cluster reports ready fast, but the first search stalls.

Net: `node_disk` correctly saves the **download** (network egress + disk write)
versus `ephemeral`, but provided **zero RAM warmth** on restart.

## User-facing change

No UI change. On a `node_disk` / `data_disk` restart, the warmup Job now reads
the shard into the node page cache itself (off the first search's critical
path), so the first BLAST search after a stop/start no longer pays the full
cold-cache `vmtouch` cost. The warmup row now transitions through the
`touching_memory` ("Touching files into RAM") phase during that read, then to
`completed`.

## API / IaC diff summary

- [api/services/warmup/scripts.py](../../../api/services/warmup/scripts.py):
  `warmup_shell_command()` — on the `DOWNLOAD_SKIP` branch only (the download
  branch already warms the cache as a side effect), run an inline
  `blastdb_path -getvolumespath | xargs vmtouch -tqm <budget>` step. It is
  **self-adapting** (real work on a cold node_disk cache; a fast noop on an
  already-warm cache), **best-effort** (`|| true`, never fails staging), and
  opt-out via `ELB_WARMUP_VMTOUCH_DISABLE=1`. The budget mirrors the search-pod
  vmtouch (60% of `MemAvailable`, per-file cap). Hardened after a 3-round
  bug/risk critique:
  - **Observable no-op**: when the warmup image lacks `vmtouch` / `blastdb_path`,
    when the step is disabled, or when volume paths cannot be resolved, the
    entrypoint now logs a `VMTOUCH_SKIP …` reason instead of skipping silently —
    so an operator can tell RAM was not pre-warmed rather than wondering why the
    first search is still cold.
  - **Budget floor + fallback**: the budget floors to `>=1G` and falls back to a
    fixed `4G` when `MemAvailable` is absent/zero, so the warm never degrades to
    a silent `-m 0G` / `-m ''` noop.
  - **Empty volume list guarded**: an empty `blastdb_path` result is logged and
    skipped rather than running `vmtouch` with no args.

  Module docstring updated.
- [api/services/warmup/jobs.py](../../../api/services/warmup/jobs.py):
  `_phase_from_warmup_log()` maps the new `VMTOUCH_WARM` log token to the
  existing `touching_memory` phase (checked after the `done shard=` completed
  matcher, so a finished pod still resolves to `completed`).
- [api/tests/test_warmup_jobs.py](../../../api/tests/test_warmup_jobs.py): four
  regression tests — `test_warmup_skip_path_warms_page_cache_with_vmtouch`
  (vmtouch present, in the skip branch only, best-effort, no ConfigMap script),
  `test_warmup_skip_path_logs_when_vmtouch_unavailable` (all three `VMTOUCH_SKIP`
  reasons logged), `test_warmup_skip_path_budget_has_floor_and_fallback` (budget
  floor + fixed fallback + empty-path guard), and
  `test_vmtouch_warm_log_maps_to_touching_memory_phase` (in-flight →
  `touching_memory`; completed → `completed`).

## Validation evidence

- `uv run pytest -q api/tests/test_warmup_jobs.py` → 42 passed.
- `uv run pytest -q api/tests -k "warmup or staging"` → 191 passed.
- `uv run ruff check api/services/warmup/scripts.py api/services/warmup/jobs.py
  api/tests/test_warmup_jobs.py` → all checks passed.
- Rendered `warmup_shell_command()` passes `bash -n`; the vmtouch block sits
  inside the `DOWNLOAD_SKIP` else branch. The budget logic was exercised live:
  it resolves to 60% of `MemAvailable` on a normal host and falls back to `4G`
  when `MemAvailable` is absent.
- **Live-cluster validation pending** — the wall-clock first-search improvement
  on a real `node_disk` stop/start cycle needs to be measured on a deployed
  cluster before this is considered fully verified.

## Scope / follow-ups (not in this change)

The physical disk→RAM read cost is unchanged; this change moves it off the
first-search critical path. Larger throughput improvements remain available:

- Pin the `node_disk` blastpool OS disk to a Premium SSD tier so the disk→RAM
  read is faster (currently `cluster_params.py` sets `os_disk_type="Managed"`
  + 512 GB without an explicit performance SKU).
- Finish the `data_disk` (Premium SSD v2 / Ultra PVC) warm-cache path (today it
  falls back to ephemeral).
- Increase node/shard count: each node vmtouches only its shard, so warmup time
  scales down with more nodes.
