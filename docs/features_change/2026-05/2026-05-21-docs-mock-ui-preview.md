# Docs Mock UI Preview

## Motivation

Researchers and reviewers need a way to open the control plane UI from the documentation site without live Azure resources or a running backend.

## User-Facing Change

- Added a UI Preview guide page with links to a static mock Control Plane build.
- Added scenario links for ready dashboard, first-run setup, stopped AKS, database warmup, New Search, Jobs, Results, and API Reference.
- Added a docs-only mock mode that seeds workspace configuration and serves fixture responses through a fetch interceptor.
- Added docs-only EventSource mocking so the static preview can render sidecar live snapshots without calling a real SSE backend.
- Added mock HTTP request inspector samples and a direct `inspector=http` preview link for the Sidecar Runtime modal.
- Added a docs build script that compiles the Vite app into `docs/mock-app` with hash routing for GitHub Pages compatibility.

## API / IaC Diff Summary

- No API changes.
- No infrastructure changes.
- GitHub Pages documentation workflow now installs web dependencies and builds the mock preview before `mkdocs build`.

## Validation Evidence

- Passed: `bash scripts/docs/build-mock-preview.sh` generated `docs/mock-app/index.html` and hashed Vite assets. Vite emitted only the existing large-chunk warning.
- Passed: `uv run mkdocs build` copied the generated mock app into `site/mock-app`.
- Passed: artifact smoke check confirmed `docs/mock-app/index.html`, `site/mock-app/index.html`, the UI Preview guide links, and the mock app asset references.
- Passed: local MkDocs smoke on `http://127.0.0.1:8013/elb-dashboard/mock-app/#/?scenario=ready` rendered the dashboard with zero ErrorBoundary matches in Playwright.
- Passed: local MkDocs smoke on `http://127.0.0.1:8013/elb-dashboard/mock-app/#/blast/submit?scenario=ready` rendered New Search for 17 seconds with zero ErrorBoundary matches after completing the mock `warmup_plan` contract.
- Passed: local MkDocs smoke on `http://127.0.0.1:8013/elb-dashboard/mock-app/#/?scenario=ready&inspector=http` opened the HTTP request inspector modal with 7 mock captured requests, `/api/blast/pre-flight` visible, and zero ErrorBoundary matches in Playwright.