# prepare-db AKS-fanout: surgical per-file repair (delete + re-copy poisoned blobs)

## Motivation

Large BLAST DB downloads (`nt`, `core_nt`) kept failing on the AKS-fanout
prepare-db path and never converged, surfacing in the dashboard as a
perpetual `partial · N failed` / red error state that "계속 오류가 해결되지
않고" (kept not resolving) across 10+ attempts.

## Root cause (verified live 2026-06-05 on cluster-02)

Some destination blobs carry a **corrupt UNCOMMITTED block list** left by an
earlier interrupted upload (legacy streamed runs or killed transfers). When
azcopy's server-side `Put Block From URL` stages new blocks onto that blob it
fails with:

```
RESPONSE Status: 400 The specified blob or block content is invalid.
X-Ms-Error-Code: InvalidBlobOrBlock
COPYFAILED: .../nt_euk.135.nsq : When Staging block from URL.
```

This is a **permanent per-blob error**, not a transient one:

- `--overwrite=true` does NOT clear it — it stages MORE blocks onto the bad
  list, so the file fails every time (a single-file copy of `nt_euk.135.nsq`
  failed in 6 s, repeatedly).
- A failed transfer makes azcopy return exit 1 (`CompletedWithErrors`) for the
  whole shard even when 485/486 committed fine.
- Re-running the whole glob does NOT skip the good blobs — for S3→Blob,
  `--overwrite=ifSourceNewer` reports `Skipped=0` and re-downloads the entire
  ~200 GB shard, then hits the same poisoned blob again. **Non-convergent.**

The small `16S_ribosomal_RNA` DB (18 MB) never hit a poisoned blob, so it
always succeeded — which is why only the large DBs were stuck.

### Why the previous fix (commit cc08446) did not work

The first fix added an in-pod retry loop using `--overwrite=ifSourceNewer`,
assuming the retry would skip committed blobs and re-fetch only the failed
one. **Live evidence disproved that assumption**: the progress line showed
`Skipped=0` — every retry re-downloaded all 200 GB and hit the same poisoned
blob. The assumption was never verified before deploy. This change replaces it
with a verified approach.

## The fix (every primitive verified live before deploy)

After the glob copy, if any transfers failed:

1. Extract ONLY the failed files from `azcopy jobs show <jid> --with-status=Failed`
   (gives exact `source` + `destination` URLs).
2. **Delete** each bad destination blob (`azcopy remove`) — Delete Blob
   removes both committed and uncommitted blocks, clearing the poison.
3. Re-copy that single file directly (no glob, no re-scan, no re-download of
   the good blobs).
4. Bounded by `ELB_AZCOPY_MAX_ATTEMPTS` repair rounds on the shrinking failed
   set; the pod exits non-zero only if a file still fails after all rounds.

### Live verification (cluster-02, Storage stayed Disabled)

- `azcopy jobs show --with-status=Failed` returns the exact failed source/dest
  pair.
- single-file copy onto the poisoned blob → `400 InvalidBlobOrBlock` (6 s),
  reproducibly.
- `azcopy remove` + single-file re-copy → `1 Done, 0 Failed` in ~37 s.
- Full repair orchestration in a throwaway pod (python parser + mapfile loop +
  remove + re-copy of two real nt files) → `parsed 2 pairs` → `REPAIR ok` ×2 →
  `PROBE DONE phase=repair exit=0` in ~31 s.
- `azcopy jobs resume` was tested and REJECTED: it panics on S3Blob jobs
  (Go nil-deref in `getCredentialTypeForLocation`).

## Code / behaviour change

`api/services/k8s/prepare_db_jobs.py` — `PREPARE_DB_AKS_SCRIPT` now runs the
glob copy, then on failure performs the delete-+-re-copy surgical repair loop
described above. No manifest, RBAC, network, or Storage-posture change.
Storage stays `publicNetworkAccess: Disabled`.

## Validation

- `uv run ruff check api` — clean.
- `uv run pytest -q api/tests` — 2900 passed, 3 skipped.
- New regression test `test_script_repairs_only_failed_files_not_full_redownload`
  locks the repair design (`--with-status=Failed`, `azcopy remove`, bounded
  `while [ "${#PAIRS[@]}" -gt 0 ]` loop, no `ifSourceNewer` overwrite mode).

## Deploy note

Baked pod-script change → requires `api`+`worker` image rebuild
(charter §13 redeploy exception: pod-script bug, not reproducible in Tier 1
pytest / Tier 2a uvicorn). After deploy, re-dispatch the affected DBs
(`nt`, `core_nt`) so a fresh Job picks up the new script.
