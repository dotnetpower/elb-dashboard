---
title: Warmup re-downloads a corrupt DB cache instead of skipping onto it
description: The warmup skip decision now runs a blastdbcmd integrity probe so a vol/lmdb-mismatch cache is re-downloaded rather than failing the search.
tags:
  - blast
  - operate
---

# Warmup blastdbcmd integrity gate

## Motivation

A `core_nt` warmup shard failed repeatedly on the cluster with:

```
DOWNLOAD_SKIP existing shard=09
BLAST Database error: Input db vol does not match lmdb vol
```

The warmup script's cache self-heal only invalidated the `.download-complete`
marker when **no** `*.nsq` volume files were present (or the source version
drifted). A cache whose volume files *exist* but disagree with the alias/LMDB
metadata — a partially-overwritten or truncated earlier download — passed those
checks, so the script hit `DOWNLOAD_SKIP` and then failed the `blastdbcmd -info`
step with `Input db vol does not match lmdb vol`. The shard never re-downloaded
and stayed cold across every retry (`BackoffLimitExceeded`).

## User-facing change

Warmup now probes the staged DB's integrity **before** deciding to skip. If
`blastdbcmd -db "$ELB_DB" -info` fails, the `.download-complete` marker is
invalidated and the shard is re-downloaded cleanly, self-healing a corrupt
cache. A healthy cache probes in well under a second (local metadata only), so
there is no measurable warmup slowdown, and a genuinely healthy shard is never
re-downloaded.

## API / IaC diff summary

* `api/services/warmup/scripts.py` — the integrity gate is added to both the
  warmup Job entrypoint (`warmup_shell_command`) and the ConfigMap init script
  (`INIT_DB_SHARD_AKS_SCRIPT`), immediately before the `DOWNLOAD_SKIP` branch:

  ```bash
  if [ -f .download-complete ]; then
      if ! blastdbcmd -db "$ELB_DB" -info >/dev/null 2>&1; then
          log "CACHE_CORRUPT blastdbcmd integrity probe failed - invalidating"
          rm -f .download-complete
      fi
  fi
  ```

* `terminal/patch_elastic_blast.py` — the same gate is mirrored into the
  submit-time hardened init script (`_HARDENED_INIT_DB_SHARD_AKS_SCRIPT`) so a
  full `elastic-blast submit` that overwrites the shared `elb-scripts` ConfigMap
  keeps the self-heal.
* No IaC change. The scripts are baked into the api/worker (warmup) and terminal
  (submit) images, so both images must be rebuilt for the fix to take effect
  live.

## Validation

* `uv run pytest -q api/tests/test_warmup_jobs.py api/tests/test_terminal_patch_elastic_blast.py`
  — new assertions confirm the `blastdbcmd -db "$ELB_DB" -info` gate is present
  in the warmup entrypoint and the hardened init script; full suites green.
* `uv run ruff check api/services/warmup/scripts.py` — clean.
