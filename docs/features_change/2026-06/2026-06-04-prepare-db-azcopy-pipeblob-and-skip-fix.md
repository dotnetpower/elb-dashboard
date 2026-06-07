# prepare-db AKS-fanout: azcopy PipeBlob syntax + idempotency skip fixes

**Date:** 2026-06-04
**Area:** BLAST DB download (AKS-fanout `mode=aks` prepare-db path)
**Files:** `api/services/k8s/prepare_db_jobs.py`, `api/tests/test_prepare_db_aks_manifest.py`

## Motivation

The fast AKS-fanout `nt` download appeared to run (10 shard pods `Running`) but
made no progress — the dashboard sat at 0%. Investigation revealed **two
independent defects** in the pod-side streaming script
(`PREPARE_DB_AKS_SCRIPT`), both of which silently produced a broken result:

1. **Zero-byte-upload outage (BUG 1).** Each file was streamed with
   `curl ... | azcopy copy --from-to=PipeBlob "" "$dst_url"`. `azcopy` >= 10.32
   rejects the two-positional form: it parses the empty first positional `""`
   as the *source* and aborts the copy immediately with a non-zero exit and
   **zero bytes transferred**. `curl` then receives SIGPIPE and logs
   `curl: (23) Failure writing output to destination`. Every file failed; the
   `curl --retry 5 --retry-delay 30` cadence made the errors accumulate slowly
   so the pods looked "busy" while uploading nothing.

2. **Corrupt-DB wrong-skip (BUG 2).** The per-file idempotency check used
   `azcopy list ... | grep -q '"ContentLength"'` — i.e. it skipped a file
   whenever the destination blob merely **had** a `ContentLength` key. A 0-byte
   placeholder left behind by an aborted legacy server-side copy *also* has that
   key (value `0`), so ~1,144 incomplete blobs (978 zero-byte + 166 < 1 KiB out
   of 4,815) under `nt/` were wrongly treated as "already uploaded", leaving a
   corrupt BLAST database.

## User-facing change

- The AKS-fanout BLAST DB download (`Get` on the Storage card → `mode=aks`) now
  actually transfers bytes on `azcopy` >= 10.32 instead of silently uploading
  nothing.
- Re-running prepare-db over a partially-downloaded DB now **re-fetches**
  truncated / 0-byte blobs instead of skipping them, so an interrupted download
  self-heals on the next run rather than producing a corrupt database.

## Code change summary

`api/services/k8s/prepare_db_jobs.py` (`PREPARE_DB_AKS_SCRIPT`):

- **BUG 1 fix:** destination-first single positional —
  `azcopy copy "$dst_url" --from-to=PipeBlob` (stdin is the implicit source).
  Added an explanatory comment forbidding reintroduction of the empty `""`
  placeholder.
- **BUG 2 fix:** the idempotency check now skips a file **only** when the
  destination blob's `ContentLength` is strictly `> 0`. Missing blobs (empty
  listing), 0-byte placeholders, and parse-fail all fall through to a clean
  re-download (azcopy overwrites).
- Extracted the duplicated `azcopy list ... | python3` ContentLength parser into
  a single `blob_content_length()` shell helper shared by the idempotency check
  and the post-upload verify step. It echoes the integer length, nothing when
  the blob is absent, or the literal `PARSE_FAIL` on schema drift.

`api/tests/test_prepare_db_aks_manifest.py`:

- New `test_idempotency_skip_requires_nonzero_content_length` — asserts the
  brittle `grep -q '"ContentLength"'` key-presence check is gone, that skip is
  gated on `[ "$existing_len" -gt 0 ]` via `blob_content_length`, and that
  `PARSE_FAIL` does not count as "already uploaded".
- Existing `test_pipeblob_destination_is_single_positional` (BUG 1 guard) and
  `test_script_skips_already_uploaded_blobs` remain green.

## Live recovery (this incident only — ephemeral)

The in-flight `nt` job was recovered without waiting for a redeploy:

1. Listed `nt/` blobs from inside an AKS pod (text `azcopy list`, parsed
   `Content Length:`), built a delete-list of the 1,144 blobs < 1 KiB.
2. Transferred the list into a pod via base64 over `kubectl exec` (the pod image
   ships no `tar`, so `kubectl cp` fails) and ran
   `azcopy remove --list-of-files`. Result: 4,815 → 3,671 fully-complete blobs,
   0 zero-byte, 0 < 1 KiB.
3. Deleted and recreated the Indexed Job with the PipeBlob-fixed ConfigMap so the
   1,144 missing blobs re-download cleanly.

> The live ConfigMap patch is ephemeral. The baked api/worker image still ships
> the old `PREPARE_DB_AKS_SCRIPT`, so an image rebuild + redeploy is required for
> the source fix to survive the next prepare-db dispatch.

## Validation evidence

- **BUG 1, real file:** `curl nt.163.nsq (3 GB) | azcopy copy "$DST"
  --from-to=PipeBlob --block-size-mb=64` → `PIPELINE OK`, 2.79 GiB uploaded in
  ~3 min. The two-positional form exited non-zero with no transfer.
- **BUG 2 + recovery:** post-cleanup blob scan = `blobs=3671 zero=0 under1KiB=0
  ok=3671`. After Job recreate, blob count climbed `3671 → 3674` with
  `total pipeline errors across shards: 0`, confirming real uploads resumed.
- **Unit:** `uv run ruff check` clean; `uv run pytest
  api/tests/test_prepare_db_aks_manifest.py
  api/tests/test_prepare_db_aks_planner.py` → 36 passed.

## Follow-up

- **Redeploy for persistence** (warranted exception to "do not redeploy" — the
  bug is in the baked pod script): rebuild api/worker image via `az acr build` /
  `quick-deploy.sh`. moonchoi target needs explicit MSAL overrides because `.env`
  carries demo values.
- **Optimization (not done):** the per-file skip check issues one `azcopy list`
  per file (~1.5 s each → ~12 min/shard just to scan 480 files before
  downloads). A single recursive container listing into a lookup set would cut
  the scan to seconds. Left as a future change to keep this fix minimal.
