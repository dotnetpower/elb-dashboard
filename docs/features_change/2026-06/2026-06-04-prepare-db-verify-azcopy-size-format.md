---
title: Fix prepare-db verify false-failures under azcopy 10.32
description: Normalize azcopy's human-readable ContentLength to bytes, compare with tolerance, and stop azcopy remove from draining the shard file list.
tags:
  - blast
  - infra
---

# Fix prepare-db verify false-failures under azcopy 10.32

## Motivation

The `nt` prepare-db AKS Job (`prepare-db-nt-260602010502`) reported
`Failed 0/10` (BackoffLimitExceeded, 12 pods in Error) while it looked, from
the dashboard, like it was "still downloading". Each shard pod copied files
1-9 cleanly, then on the first integrity-sampled file (every
`VERIFY_EVERY_N`-th, i.e. file 10 — a ~3 GB `nt_euk.021.nsq` volume) logged:

```
ERROR size mismatch .../nt_euk.021.nsq exp=2999987448 got=2.79 GiB
DONE shard=04 ok=9 fail=1 skip=0
```

Two distinct bugs combined:

1. **Human-readable size compare.** `azcopy` auto-upgraded to **10.32.4**,
   whose `azcopy list --output-type=json` reports `ContentLength` as a
   human string (`"2.79 GiB"`) instead of a raw integer. The verify step
   compared that string byte-for-byte against the raw NCBI `Content-Length`
   (`2999987448`), so every multi-GB sampled file false-failed — even though
   `2999987448 bytes == 2.79 GiB` and the upload was correct. On a false
   mismatch the script ran `azcopy remove`, **deleting the healthy blob**.

2. **Loop input drained by `azcopy remove`.** The shard loop read the file
   list on stdin (`done < "$FILE_LIST"`). `azcopy remove` was invoked without
   an stdin redirect and azcopy drains fd 0, so the first remove swallowed the
   remaining file-list lines and the loop ended after one file — hence
   `ok=9 fail=1` and an immediate `DONE` instead of continuing through the
   shard.

## User-facing change

* prepare-db downloads no longer false-fail (and no longer delete correct
  blobs) on integrity-sampled multi-GB files under azcopy >= 10.32.
* A shard now processes its entire file list even when a genuine mismatch
  triggers an `azcopy remove`.
* Genuine truncations / HTML error bodies are still caught (they differ from
  the expected size by far more than the tolerance).

## Code change summary

`api/services/k8s/prepare_db_jobs.py` (`PREPARE_DB_AKS_SCRIPT`):

* `blob_content_length()` parser now normalizes `ContentLength` to an integer
  byte count whether azcopy emits a raw integer (<= 10.31) or a human string
  (`"2.79 GiB"`, `"512 B"`, …, binary and decimal units), returning
  `PARSE_FAIL` on shapes it cannot interpret.
* The post-upload verify replaces exact string equality with a **1% / 1 KiB
  tolerance** byte compare (`abs(up - exp) <= max(1024, exp // 100)`) to absorb
  the ~3-significant-figure precision loss of the human format.
* The shard loop reads the file list on **fd 3** (`read -r KEY <&3` /
  `done 3< "$FILE_LIST"`) and `azcopy remove` gets `</dev/null`, so no in-loop
  azcopy call can drain the loop's input.

## Validation

* `uv run pytest -q api/tests/test_prepare_db_aks_manifest.py` — 32 passed,
  including new functional tests that extract and run the shipped parser
  (`"2.79 GiB"` → `2995639357`, raw integer preserved, units, PARSE_FAIL) and
  the tolerance snippet (rounded size passes, half-size truncation fails), plus
  guards for the fd-3 loop and `azcopy remove </dev/null`.
* `uv run pytest -q api/tests/test_prepare_db_aks_task.py` — 11 passed.
* `uv run ruff check api/services/k8s/prepare_db_jobs.py api/tests/test_prepare_db_aks_manifest.py` — clean.

## Rollout note

The script is baked into the ConfigMap by the Celery worker at Job-submit
time, so the fix only takes effect after the **api/worker image is
redeployed** and the `nt` prepare-db is **re-run** (the failed Job deleted the
sampled blobs it touched). This is a legitimate redeploy/re-run case
(AKS-job toolchain change per charter §13), tracked separately from the code
change.
