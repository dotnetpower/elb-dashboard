# Overview researcher copy

## Motivation

The public Overview page described the control plane by its implementation stack before explaining why researchers would use it. The page needed to lead with the pain it removes from cloud BLAST workflows.

## User-Facing Change

- Reframed the Overview opening around running large BLAST searches without becoming the cloud operator.
- Added a researcher-first workflow that explains the path from workspace readiness to completed results.
- Moved architecture-heavy links into a platform maintainer section.

## API/IaC Diff Summary

- No API or infrastructure changes.
- Documentation-only copy update on the MkDocs home page.

## Validation Evidence

- `uv run mkdocs build` passed.