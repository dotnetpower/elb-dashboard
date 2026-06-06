---
title: BLAST DB path uses the blob directory, not the filename base
description: Fix the "Downloaded but not available" mismatch for nested subset databases (nt/nt_euk) by deriving the DB prefix from the blob's real directory.
tags:
  - blast
  - user-guide
---

# BLAST DB path uses the blob directory, not the filename base

## Motivation

Session-browser testing of the New Search flow surfaced a hard, user-facing
contradiction:

- The New Search database list rendered `nt_viruses`, `nt_euk`, `nt_others`,
  and `nt_prok` as **"Downloaded · ready"**.
- Selecting any of them and running **Check Readiness** failed the submit
  pre-flight with `BLAST database 'nt_euk' is not available in Storage.
  Expected BLAST DB files under blast-db/nt_euk/nt_euk*.`

Inspecting the live `blast-db` container showed those subset DBs do exist —
but as blobs **inside the `nt/` folder** (`nt/nt_euk.000.nsq`, …), not under a
top-level `nt_euk/` folder. They were staged alongside the parent `nt` DB.

## Root cause

`list_databases` registered every BLAST-extension blob by its **filename base**
and built the reconstruction `prefix` from that base only:

```python
prefix = f"custom_db/{base}" if is_custom else base   # before
```

For folder-layout DBs where the folder name equals the base (`core_nt/core_nt.*`)
this happened to be correct. For nested subset DBs it was wrong: `nt/nt_euk.000.nsq`
has base `nt_euk` but lives in directory `nt`, so the prefix `nt_euk` produced the
frontend path `blast-db/nt_euk/nt_euk` — a path that does not exist. The submit
pre-flight (`validate_blast_database_available`) re-derives the blob prefix from
that URL, listed `nt_euk/nt_euk*`, found nothing, and reported the DB as missing.

## User-facing change

The DB `prefix` is now the **actual blob directory**, so the dashboard never
offers a database that fails the submit pre-flight:

| Blob layout | DB name | prefix (before) | prefix (after) | path |
| --- | --- | --- | --- | --- |
| `nt/nt.00.nsq` | `nt` | `nt` | `nt` | `blast-db/nt/nt` |
| `nt/nt_euk.000.nsq` | `nt_euk` | `nt_euk` ❌ | `nt` ✅ | `blast-db/nt/nt_euk` |
| `core_nt/core_nt.00.nin` | `core_nt` | `core_nt` | `core_nt` | `blast-db/core_nt/core_nt` |
| `custom_db/labdb/labdb.nsq` | `labdb` | `custom_db/labdb` | `custom_db/labdb` | `blast-db/custom_db/labdb/labdb` |
| `standalone.nsq` (top-level) | `standalone` | `standalone` ❌ | `` (empty) ✅ | `blast-db/standalone` |

`buildDatabasePath` now drops empty path segments so a top-level DB file
(`prefix == ""`) resolves to `blast-db/standalone` instead of the
double-slash `blast-db//standalone`, which also previously failed the
pre-flight.

## API / IaC diff summary

- `api/services/storage/database_list.py`: `prefix` is now `"/".join(parts[:-1])`
  (the blob directory) instead of the filename base. The `is_custom` branch is
  removed because the directory already encodes `custom_db/<db>`. No response
  field added or removed — only the existing `prefix` value is corrected.
- `web/src/pages/blastSubmit/helpers.ts`: `buildDatabasePath` joins
  `[container, prefix, name]` with empty segments filtered out, keeping the
  `?? name` fallback for older responses that omit `prefix`.
- No infra change.

## Validation evidence

- `uv run pytest -q api/tests` → 3022 passed, 3 skipped.
- New backend regression: `test_list_databases_prefix_is_blob_directory_not_filename_base`
  asserts directory prefixes for folder / nested / custom / top-level layouts.
- New frontend tests in `programSelection.test.ts` `describe("buildDatabasePath")`
  cover folder, nested subset (`nt/nt_euk`), custom multi-segment, empty-prefix
  top-level, and undefined-prefix fallback (28 tests pass).
- `cd web && npm run build` → built in 9.22s.
- `uv run ruff check` on touched files → all checks passed.

### Live verification (deployed)

The `nt/nt_euk` blob layout that triggers this bug only exists in the deployed
storage account, so the fix was verified against the live control plane after
deploying the `api` sidecar (revision `ca-elb-dashboard--0000184`, image tag
`20260606214808`):

- `GET /api/blast/databases` now returns `prefix: "nt"` for `nt_euk`,
  `nt_others`, `nt_prok`, and `nt_viruses` (was the filename base before).
- `POST /api/blast/pre-flight` with `storage_account` set runs the real
  file-existence gate `validate_blast_database_ready`. Contrast proves the fix:
  - new path `blast-db/nt/nt_viruses` → `database` check **pass** —
    "Database 'blast-db/nt/nt_viruses' is available (nt/nt_viruses.00.nsq)".
  - old path `blast-db/nt_viruses/nt_viruses` → `database` check **fail** with
    the original `database_not_found` error, confirming the bug was real.

Only the `api` sidecar was redeployed: the deployed frontend already builds
`blast-db/nt/nt_viruses` correctly for non-empty prefixes, so the api-only
deploy fully fixes the user-facing "Downloaded but not available" failure. The
`buildDatabasePath` empty-segment change is defensive for hypothetical
top-level DB files (none exist in this environment) and ships with the next
routine frontend build.
