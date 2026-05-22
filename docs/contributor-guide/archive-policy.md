---
title: Archive Policy
description: When and how to move time-bound docs (security follow-ups, in-progress research plans) out of the live navigation into the docs/_archive folder.
tags:
  - contributor
---

# Archive Policy

Some pages on this site are **time-bound** — security audit follow-ups,
in-progress implementation plans, research notes that informed a now-shipped
feature. Leaving them in the live navigation forever gives readers the wrong
impression ("still open?"). This page defines how to retire them.

## When to archive

Move a page out of the live nav when **all** of the following are true:

1. Every actionable item on the page has shipped (or has its own current
   tracking issue / change note).
2. The page is no longer the source of truth for anything that operators,
   researchers, or contributors need at runtime.
3. At least one current page explains "where the design landed" and links to
   the archived page for history.

Pages on the watch-list as of 2026-05-22:

| Page | Trigger to archive |
|------|--------------------|
| [Security Audit Follow-up (2026-05-22)](../copilot/security-audit-followup.md) | All items #1 / #2 / #4 / #8 / #15 / #16 / #19 closed or moved to GitHub issues. |
| [Web BLAST Compatibility Plan](../research/web-blast-compatibility-plan.md) | All "In progress" checkboxes completed and the contract moves into a durable Architecture page. |

## How to archive

1. Move the file: `git mv docs/<path>/<file>.md docs/_archive/<YYYY>-<short-name>.md`.
2. Add a `status: archived` field to the frontmatter; keep `title`,
   `description`, and `tags`.
3. Add a one-line callout at the top: `> Archived YYYY-MM-DD. Current page:
   [<title>](../<current-path>)`.
4. Remove the entry from `mkdocs.yml` nav.
5. Add a redirect in `mkdocs.yml`:
   ```yaml
   - redirects:
       redirect_maps:
         <old/path>.md: _archive/YYYY-<short-name>.md
   ```
6. Update any internal links that pointed at the old path.
7. The archive folder is excluded from the `frontmatter` guard's tag canon by
   default; the guard still requires `title` + `description`.

## What does *not* belong in `_archive/`

- Per-feature change notes under `docs/features_change/` — those are already
  indexed by month/category in the [Change Log](../changelog.md) and stay
  searchable forever.
- The legacy Azure Functions code tree — it was **deleted** from the
  repository on 2026-05-19, not archived. See
  [Container Apps Architecture](../architecture/container-apps.md) for the
  migration history.
