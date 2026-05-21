# API Reference Screenshot Guide

## Motivation

The API Reference user guide still had placeholder notes for future screenshot capture. Readers needed a concrete view of the in-app API menu and endpoint browser.

## User-Facing Change

- Added a captured API Reference screenshot to the user guide.
- Rewrote the guide around when to use the API Reference, how to find endpoints, how the API token panel should be treated, and how to avoid leaking sensitive values in screenshots.
- Clarified that external API calls must include `X-ELB-API-Token`, while in-page `Try` requests attach the same token internally.

## API / IaC Diff Summary

- No API changes.
- No infrastructure changes.
- Documentation-only update under `docs/user-guide/api-reference.md`, `docs/images/screenshots/api-reference.png`, and `docs/screenshot-capture-manifest.json`.

## Validation Evidence

- Captured `docs/images/screenshots/api-reference.png` from the local dashboard at `http://127.0.0.1:8090/docs` with the API menu selected and endpoint groups visible.
- Masked the visible API base URL and avoided exposing token values.
- `uv run mkdocs build`