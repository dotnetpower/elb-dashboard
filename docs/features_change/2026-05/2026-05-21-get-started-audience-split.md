# Get Started audience split

## Motivation

The Get Started page mixed the researcher quick path with maintainer validation, manual deployment, optional AKS smoke testing, and troubleshooting. The first page felt long and difficult for a researcher who only needs to deploy the control plane and open the browser dashboard.

## User-Facing Change

- Rewrote Get Started as a short researcher-oriented deployment path centered on `./deploy.sh`.
- Added Deployment Reference for platform maintainers, administrators, and developers who need the full installation, manual `azd`, redirect URI, smoke test, cleanup, and troubleshooting details.
- Added Deployment Reference to the MkDocs navigation and Overview links.

## API/IaC Diff Summary

- No API or infrastructure changes.
- Documentation-only split across `docs/get-started.md`, `docs/deployment-reference.md`, `docs/index.md`, `docs/changelog.md`, and `mkdocs.yml`.

## Validation Evidence

- `uv run mkdocs build` passed.