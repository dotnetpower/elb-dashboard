# High Level Architecture

## Motivation

The docs had detailed implementation references for Container Apps, authentication, resource-plane tasks, and the browser terminal, but no first-pass architecture map for readers who need the system shape before diving into those details.

## User-Facing Change

- Added a High Level Architecture page with a Mermaid diagram of the browser workflow, Container App sidecars, Azure resource plane, and ElasticBLAST workload plane.
- Added the new page to the Architecture navigation and linked it from the Overview page.
- Added plain Mermaid rendering support for documentation diagrams, with click-to-fullscreen viewing for larger diagrams.
- Added first-use external links for core architecture terms such as Celery, Redis, AKS, MSAL, managed identity, Azure Storage, private endpoints, WebSocket, and ttyd; documentation external links now open in a new tab.
- Added project documentation guidance so future docs updates link important external technical terms consistently on first meaningful use.

## API / IaC Diff Summary

- No API changes.
- No infrastructure changes.
- Documentation-only update under `.github/copilot-instructions.md`, `docs/high-level-architecture.md`, `docs/index.md`, `docs/changelog.md`, `docs/javascripts/external-links.js`, `docs/javascripts/mermaid-init.js`, `docs/stylesheets/mermaid.css`, and `mkdocs.yml`.

## Validation Evidence

- `uv run mkdocs build` completed successfully.
- Local MkDocs page check returned the High Level Architecture page with the Mermaid runtime and initializer scripts: `http://127.0.0.1:8012/elb-dashboard/high-level-architecture/`.
- Browser render check confirmed one plain `.mermaid svg`, no custom frame or zoom controls, and zero Mermaid parse-error elements on the High Level Architecture page.
- Browser interaction check confirmed the diagram opens a larger fullscreen SVG view that fits the viewport width, opens from keyboard focus with `Enter`, and closes with `Escape`.
- Generated HTML check confirmed external terminology links for Celery, Redis, MSAL, and AKS on the High Level Architecture page.
- Browser runtime check confirmed documentation external links, including Celery, receive `target="_blank"` with `rel="noopener noreferrer"`, while internal documentation links remain same-tab.