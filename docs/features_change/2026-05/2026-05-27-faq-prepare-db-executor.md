# 2026-05-27 — FAQ: clarify who downloads BLAST databases from NCBI

## Motivation

A recurring question from operators and reviewers: when a user clicks
"Download" on a BLAST database card, does the work happen on the `api`
sidecar, the `terminal` sidecar, or inside the AKS cluster? The existing
FAQ in [docs/index.md](../../index.md) covered SAS posture and sign-in
but not the prepare-db data path, so the answer was buried in
[api/routes/storage/prepare_db.py](../../../api/routes/storage/prepare_db.py).

## User-facing change

Added one Q&A to the home-page **Frequently Asked Questions** section
(rendered + `FAQPage` JSON-LD), placed right after the SAS posture
question:

> **Who actually downloads BLAST databases from NCBI — the `api` sidecar,
> the `terminal` sidecar, or AKS?**
>
> None of them transfer the bytes. The `api` sidecar's
> `POST /api/storage/prepare-db` route orchestrates the work by issuing
> per-file Azure Blob server-side copies (`start_copy_from_url`) from the
> public NCBI BLAST S3 mirror straight into the workload Storage account's
> `blast-db` container. Azure Storage itself performs the copy …

## API / IaC diff summary

- Docs only. No code, no Bicep, no route changes.
- [docs/index.md](../../index.md) — added the question to both the
  `jsonld:` frontmatter (`FAQPage.mainEntity`) and the visible
  `## Frequently Asked Questions` section.

## Validation

- `uv run mkdocs build --clean` — clean build, 0 warnings (plugin
  upsell line excluded from grep).
- Parsed the generated `site/index.html` and confirmed the
  `FAQPage` JSON-LD now lists **7** questions including the new
  "Who actually downloads BLAST databases from NCBI …" entry, and
  that the same string is present in the rendered HTML body.
