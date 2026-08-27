---
title: NCBI cloud mirror update pending
description: Surface newer NCBI FTP database releases while preventing updates from re-copying an older cloud snapshot.
tags: [blast, ui]
---

# NCBI cloud mirror update pending

## Motivation

The dashboard treated the [NCBI BLAST database S3 mirror](https://registry.opendata.aws/ncbi-blast-databases/) as both the download source and the sole update authority. On 2026-08-27 its `latest-dir` object was still `2026-07-21-01-05-02`, last modified on 2026-07-23, while the authoritative [NCBI FTP distribution](https://ftp.ncbi.nlm.nih.gov/blast/db/v5/) published a newer `core_nt` release with `last-updated=2026-08-19`, 84 volumes, and 282,692,127,129 bytes. The stored cloud snapshot has `last-updated=2026-07-18`, 80 volumes, and 269,739,189,502 bytes.

Because the cloud signature still matched the downloaded snapshot, `/api/blast/databases/check-updates` returned an authoritative empty update list and the UI hid every Update indication. Simply treating the FTP release as actionable would be unsafe: prepare-db copies raw files from the cloud mirror and would copy the old July generation again.

## User-facing change

- A downloaded database with a newer FTP release now shows an Update button in a disabled pending state instead of looking current.
- The tooltip names the FTP publication date and explains that Update will become available when the NCBI cloud mirror catches up.
- The Storage summary and database modal show a pending-update count.
- The header now labels the displayed value `NCBI cloud snapshot` instead of the misleading `NCBI latest`.
- Opening or refreshing the database manager refreshes both Storage metadata and update detection, allowing pending state to become actionable without a page reload after the cloud mirror advances.

## API and infrastructure summary

- Added a bounded, 30-minute TTL cache for NCBI's `blastdb-metadata-1-1.json` FTP release index. The fixed URL fetch uses a 30-second timeout, a 2 MiB response cap, a 1,000-entry cap, defensive copies, and does not cache failures.
- `GET /api/blast/databases/check-updates` retains `updates_available` for cloud-copyable releases and adds `updates_pending` plus `updates_pending_evaluated` for newer FTP releases awaiting the cloud mirror.
- FTP lookup failure degrades to the existing S3 update result and never blocks an actionable cloud update.
- No FTP archive download path, infrastructure, RBAC, network, authentication, or Storage setting changed.

## Validation

- `uv run pytest -q api/tests/test_ncbi_releases.py api/tests/test_blast_databases_check_updates.py` - `11 passed`.
- `uv run mypy --strict --follow-imports=skip api/services/ncbi_releases.py` - passed.
- `uv run ruff check api` and `uv run pytest -q api/tests` - `5320 passed, 4 skipped`.
- Real NCBI index smoke returned `core_nt` release `2026-08-19T00:00:00`, 84 volumes, 282,692,127,129 bytes, and 130,155,243 sequences.
- `npm --prefix web test`, `npm --prefix web run lint`, and `npm --prefix web run build` - `978 passed`; lint and build passed.
- VS Code browser validation and screenshot confirmed the `core_nt` Update button is visible and disabled, the tooltip reports the 2026-08-19 release and cloud-mirror wait, and both pending-count and cloud-snapshot labels render. The modal Refresh action issued a new `/api/blast/databases/check-updates` request.