# New Search

New Search is where a researcher defines a BLAST run, chooses the database and compute shape, reviews preflight checks, and submits the job.

## What To Explain

- Program selection and database choice.
- Query input and validation.
- Taxonomy filters and advanced algorithm parameters.
- Compute profile, sharding, and warmup options.
- Command preview and submit readiness.

## Screenshot Targets

Screenshots for this page are defined by this manifest target:

- `new-search-desktop`

Capture a valid draft search so the screenshot shows a realistic command preview instead of an empty form.# New Search

The New Search page prepares and submits ElasticBLAST jobs from the browser. It collects the BLAST program, database, query, taxonomy, compute, and execution settings before submission.

## Screenshot Slot

Capture target: `docs/images/screenshots/new-search-form.png`

Recommended state before capture:

- `blastn` is selected.
- A small, documented database such as `16S_ribosomal_RNA` is selected when available.
- The query section contains non-sensitive sample FASTA content.
- The command preview is visible and does not include secrets.

## Notes To Cover

- Choosing the BLAST program and database.
- Adding query content safely.
- Reading preflight checks and command preview.
- Submitting the job and following the job link.