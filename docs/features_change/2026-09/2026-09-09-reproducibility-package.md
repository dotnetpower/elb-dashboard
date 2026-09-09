---
title: Portable BLAST reproducibility package
description: Download one secret-free JSON package containing a job's canonical request, provenance, citations, workflow modules, and result manifest.
tags: [blast, user-guide]
---

# Portable BLAST reproducibility package

## Motivation

The Results page already exposed the submit settings, Methods citation, and
workflow-manager exports separately. A researcher auditing or archiving a run
had to collect them one at a time, and no single versioned package described
which result files belonged to that run.

## User-facing change

The Results header now includes **Reproducibility**. It downloads
`<job-id>-reproducibility.json` with:

- the canonical submit snapshot;
- the persisted or reconstructed provenance bundle;
- text, Markdown, and BibTeX citations;
- Nextflow, Snakemake, CWL, and WDL modules when the job records a database;
- the baked result manifest when available; and
- package/job timestamps and availability markers.

The package deliberately excludes raw FASTA, bearer or SAS material, and
execution idempotency/correlation identifiers. A missing result manifest or a
legacy job without workflow-export metadata degrades to an explicit
availability flag; it does not make the download fail.

## API change

`GET /api/blast/jobs/{job_id}/reproducibility` is an additive, owner-scoped,
read-only download endpoint. It uses the same authentication and ownership
contract as citation and workflow export routes. It does not write job state,
enqueue work, or call Azure Service Bus.

## Validation

- `uv run pytest -q api/tests/test_blast_reproducibility.py` - 5 passed.
- `npm --prefix web run build` - passed.
- Service Bus backend regression suite - 354 passed.
- Protected Service Bus source hashes remained identical to the pre-expansion
  baseline.