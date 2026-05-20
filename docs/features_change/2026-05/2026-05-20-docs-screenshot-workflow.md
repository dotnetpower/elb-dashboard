# Documentation screenshot workflow scaffold

## Motivation

The public MkDocs site needs repeatable screenshot capture before user-facing guides are populated with images. Future documentation work should not depend on ad-hoc page choices or unsafe screenshots that expose tenant-specific values.

## User-Facing Change

- Added a User Guide section with page stubs for Dashboard, New Search, Jobs, Results, Browser Terminal, and API Reference.
- Added a Contributor Guide page that defines screenshot capture preconditions, redaction rules, capture steps, and acceptance checks.
- Added a JSON capture manifest that records the initial screenshot target list, viewports, routes, wait selectors, output paths, and guide destinations.

## API/IaC Diff Summary

- No API or infrastructure changes.
- MkDocs navigation now includes User Guide and Contributor Guide entries.

## Validation Evidence

- `uv run mkdocs build` passed.