---
title: Structured database snapshot-drift verdict for BLAST parity
description: >-
  The Web BLAST parity comparator now quantifies how far a run's observed
  database statistics have drifted from the verified NCBI calibration,
  returning a match / drift / uncalibrated / unknown verdict instead of a
  bare boolean, so operators can judge whether NCBI-equivalent results are
  expected.
tags:
  - blast
  - research
---

# Structured database snapshot-drift verdict for BLAST parity

## Motivation

Result-side parity with NCBI Web BLAST holds only when the local database
snapshot matches the one NCBI's statistics were calibrated against. The biggest
single factor moving e-values and the hit-count tail is database *snapshot
drift* (`Statistics_db-num` / `Statistics_db-len` growth between NCBI refreshes).
Previously this was captured only as a `ParityReport.snapshot_drift` boolean
derived from a reference-vs-candidate XML comparison, and the calibrated
snapshot lived only as a human-readable `database_snapshot` string
(`"125,619,662 sequences; 1,041,443,571,674 bases"`). There was no machine
comparable, quantified verdict of "how far has this run drifted from the NCBI
calibration", and no explicit signal for databases that have no calibration at
all.

## User-facing change

The Web BLAST parity report now carries a structured snapshot-drift verdict for
the candidate run, with one of four statuses:

- `match` — observed DB statistics are within tolerance (0.5%) of the NCBI
  calibration; NCBI-equivalent statistics are expected.
- `drift` — observed statistics differ beyond tolerance; e-values and the
  hit-count tail may differ from NCBI Web BLAST. The verdict reports the
  signed percentage delta for sequences and bases.
- `uncalibrated` — the database has no verified NCBI Web BLAST equivalence
  baseline; parity cannot be asserted.
- `unknown` — calibration exists but the run did not report usable database
  statistics, so drift could not be measured.

This is operator-facing verification machinery (the parity comparator and its
test harness); no product default or submit behaviour changed.

## API / IaC diff summary

- `api/services/web_blast_searchsp.py`
  - `WebBlastSearchSpaceDefault` gains optional `calibrated_db_num` /
    `calibrated_db_len` numeric fields (additive, default `None`), populated
    for `core_nt` (125,619,662 sequences / 1,041,443,571,674 bases) and
    surfaced in `as_dict()`.
  - New `is_calibrated_database(database)` helper.
- `api/services/blast/snapshot_drift.py` (new) — `assess_snapshot_drift(database,
  observed_db_num, observed_db_len)` returns the structured verdict dict.
  Side-effect-free, no network/Storage.
- `api/services/blast/web_blast_parity.py` — `ParityReport` gains an optional
  `snapshot_drift_detail: dict | None` field, populated from the candidate's
  observed statistics. The existing `snapshot_drift: bool` is unchanged.

All changes are additive and backward-compatible; no existing field was removed
or renamed.

## Validation evidence

- `uv run pytest -q api/tests/test_snapshot_drift.py` — new suite, match / drift
  / uncalibrated / unknown / path-resolution branches.
- `uv run pytest -q api/tests/test_web_blast_parity_xml.py` — self-equivalence
  remains green; new `test_snapshot_drift_detail_is_populated` asserts the
  structured verdict is attached.
- `uv run pytest -q api/tests/test_blast_equivalence_evidence.py` — registry
  matrix + evidence validation still green with the new optional fields.
- `uv run ruff check api`.
