# MkDocs changelog navigation

## Motivation

Feature-change notes already exist under `docs/features_change/`, but the MkDocs site hid them from the generated documentation. Users needed a visible entry point to inspect recent changes from GitHub Pages.

## User-Facing Change

- Added a Change Log page to the MkDocs navigation.
- Linked recent feature-change notes by month.
- Included the `docs/features_change/` archive in the MkDocs build and search index while keeping individual notes out of the navigation sidebar.

## API/IaC Diff Summary

- No API or infrastructure changes.
- MkDocs configuration now exposes `changelog.md` and no longer excludes `features_change/**` from the build.

## Validation Evidence

- `uv run mkdocs build` passed.