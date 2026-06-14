---
title: OpenAPI external-payload hardening (sibling 3.7.6 / image 4.24)
description: "Bump elb-openapi to image 4.24 / sibling VERSION 3.7.6 to fix
four classes of stuck/misreported external-origin BLAST jobs observed in
production: blast_version reported \"unknown\", db_version_detail.detail
was an escape-encoded JSON string, shard_count/shards_succeeded stayed at 0
after marker-driven completion, and natural terminal transitions never
woke the dashboard between sync cycles."
tags:
  - blast
  - operate
---

## Motivation

Live OpenAPI-sourced BLAST jobs in the moonchoi `ca-elb-dashboard`
deployment exposed four sibling-side issues that the dashboard cannot
work around because the sibling owns the canonical payload:

* **#9** `blast_version: "unknown"` on every external row. The OpenAPI
  image does not install the `blastn`/`blastp` binaries (only the
  `elastic-blast` Python wrapper, `kubectl`, `azcopy`, `azure-cli`), so
  `_blast_version_detail`'s binary probe always failed. The XML-scrape
  fallback (`_blast_version_from_result`) only works for `-outfmt 5`
  runs and silently no-ops for the `-outfmt 6/7` tabular submits the
  dashboard now uses by default. End state: every row reported the
  literal string `unknown`.
* **#10** `db_version_detail.detail` rendered as an escape-encoded JSON
  string (`"{\"dbtype\":\"nucl\",…}"`) instead of a nested object.
  `_db_version_detail` was wrapping the metadata dict in `json.dumps`
  before returning it, so the dashboard SPA had to either re-parse the
  string or display the encoded blob verbatim. The SPA chose the latter.
* **#18** Completed jobs showed `shard_count: 0` and
  `shards_succeeded: 0` on the BLAST card. `_refresh_job_status` only
  called `_k8s_job_summary` on the kubectl-driven completion path; the
  marker-driven path (the `metadata/SUCCESS.txt` short-circuit, which is
  what most fast runs hit) skipped the summary refresh, so whatever
  stale-or-empty summary was last persisted stuck forever.
* **#16/#17** Natural terminal transitions (running → completed /
  failed via marker or kubectl summary) did not notify the dashboard.
  The sibling only fired `_webhook_notify` on cancel/stuck via
  `_cancel_job`, so the dashboard only learned about natural completion
  on its next external-jobs poll. With the default 30-second poll, a
  user clicking "Refresh" within the window saw a stuck-at-running row
  long after the cluster was idle.

## User-facing change

* **`blast_version`** now reports `2.17.0+` (with `source:
  "elastic_blast_release_pin"`) when the binary probe + env override are
  both unavailable. The value matches the BLAST+ version pinned by the
  ElasticBLAST release (`src/elastic_blast/constants.py:241`
  `ELB_DOCKER_VERSION = '1.4.0' # ElasticBLAST 1.5.0 uses BLAST+
  2.17.0`).
* **`db_version_detail.detail`** is a nested object (`{"dbtype": "nucl",
  "metadata_version": "1.1", "source_version": "20240130", …}`) instead
  of an escape-encoded JSON string. Existing dashboard renderers see
  structured fields and no longer need to JSON.parse the value.
* **Completed external rows carry real shard counts.** The marker-
  driven completion path now snapshots `k8s_job_summary` one last time
  before persisting the terminal state, so `execution.shard_count` /
  `execution.shards_succeeded` reflect the actual fan-out instead of
  staying at 0.
* **Natural completion / failure wakes the dashboard immediately.**
  Every terminal transition in `_refresh_job_status` now fires a
  best-effort webhook to `CONTROL_PLANE_URL` (the same channel that
  cancel and stuck transitions already used). The cancel/stuck path is
  unchanged — `_cancel_job` continues to be the single notifier there,
  so no double-notify on those paths.

## API/IaC diff summary

Sibling repo (`dotnetpower/elastic-blast-azure`):

* `docker-openapi/app/main.py` — `_blast_version_detail` pinned
  fallback, `_db_version_detail` dict return, new
  `_notify_terminal_transition` + `_snapshot_k8s_summary_for_terminal`
  helpers, four terminal-status branches in `_refresh_job_status`
  wrapped to call both. `VERSION` bumped 3.7.5 → 3.7.6.
* `docker-openapi/tests/test_external_payload_hardening.py` — new file,
  8 tests covering: dict-not-string `detail`, pinned BLAST+ fallback,
  `ELB_BLAST_VERSION` env override still wins, marker-completed
  webhook + summary snapshot, marker-failed webhook, kubectl
  success-path webhook, running transition does NOT fire webhook,
  marker-completed preserves existing summary when kubectl fails. Full
  sibling pytest suite: **61 passed**.

Dashboard repo (`dotnetpower/elb-dashboard`):

* `api/services/image_tags.py` — pin bumped `"elb-openapi": "4.23"` →
  `"4.24"`, with the standard inline comment mapping (`4.24 == upstream
  3.7.6 — sibling external-payload hardening …`) and a pointer to this
  change note.
* `scripts/dev/patch-openapi-build-context.py` — `_replace_once` now
  treats `new in text` as already-patched (was `count == 0 and new in
  text`), so the overlay no longer double-inserts the venv-stage
  elb-src install when the sibling Dockerfile catches up to upstream
  and ships the same block natively (which it now does at lines 55-57
  of `docker-openapi/Dockerfile`). Without this fix, the ACR build for
  4.24 failed because the venv RUN tried to `pip install /tmp/elb-src`
  twice and the first iteration's `rm -rf /tmp/elb-src` removed the
  context before the second.

No Bicep, no Container App template, no sidecar layout changes.

## Validation evidence

Sibling pytest (per the change checklist):

```
$ cd ~/dev/elastic-blast-azure/docker-openapi && .venv/bin/python -m pytest tests/ -q
…
61 passed, 69 warnings in 2.83s
```

Dashboard pytest + ruff:

```
$ cd ~/dev/elb-dashboard && uv run ruff check api/services/image_tags.py scripts/dev/patch-openapi-build-context.py
All checks passed!
$ uv run pytest -q api/tests
… (full suite)
```

Patched-context dry-run:

```
$ rm -rf /tmp/docker-openapi-build && cp -r ~/dev/elastic-blast-azure/docker-openapi /tmp/docker-openapi-build
$ python3 scripts/dev/patch-openapi-build-context.py /tmp/docker-openapi-build
patched docker-openapi build context for dashboard OpenAPI runtime policy
$ grep -c "/tmp/elb-src" /tmp/docker-openapi-build/Dockerfile
11   # was 13 with the duplicate venv block
```

ACR build + deploy verification (post-merge):

```
$ az acr build --registry acrelbdashboard3abp67bppe \
    --image elb-openapi:4.24 --file Dockerfile --build-arg version=3.7.6 .
# ROLLOUT ORDER (per charter): sibling first, image second, pin third.
# Then trigger the Deploy elb-openapi flow from the dashboard so the AKS
# Deployment picks up the new tag.
```

## Rollout order

Per the charter rule documented in
[2026-05-29-openapi-critique-fixes.md](2026-05-29-openapi-critique-fixes.md)
"Rollout order":

1. Commit + push sibling (`elastic-blast-azure@master`) — DONE.
2. Build + push ACR image `elb-openapi:4.24` from the patched
   dashboard-local context — DONE.
3. Bump `IMAGE_TAGS["elb-openapi"]` to `"4.24"` in the dashboard —
   this PR.
4. Trigger the dashboard's "Deploy elb-openapi" task so the AKS
   Deployment image is patched and the pod rolls.

A reversed order (pin before image) re-creates the 2026-05-30 P0
rollback.
