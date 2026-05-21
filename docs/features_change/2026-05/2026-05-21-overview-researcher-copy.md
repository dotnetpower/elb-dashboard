# Overview researcher copy

## Motivation

The public Overview page described the control plane by its implementation stack before explaining why researchers would use it. The page needed to lead with the pain it removes from cloud BLAST workflows.

## User-Facing Change

- Reframed the Overview opening around running large BLAST searches without becoming the cloud operator.
- Added an explicit audience section for teams that need repeatable browser-first BLAST work on Azure.
- Added an ElasticBLAST on Azure note explaining that the sibling runtime repository is the Azure-portable ElasticBLAST execution layer automated by this dashboard.
- Added a concrete first-run scenario and primary Overview actions for Get Started and High Level Architecture.
- Added a Before/With comparison table to make the removed operational burden easier to scan.
- Clarified the operational burden the control plane removes: scattered readiness checks, local terminal command assembly, direct Storage exposure, and result-file hunting.
- Added a three-screenshot Dashboard tour for the cluster plane, resource plane, and live monitoring surfaces.
- Added short captions under each Dashboard screenshot tab so readers know what to inspect in each view.
- Added click-to-fullscreen viewing for documentation images, including the Dashboard tour screenshots.
- Added a researcher-first workflow that explains the path from workspace readiness to completed results.
- Added a short design-differentiators section covering Azure Container Apps sidecars, managed identity, API-streamed Storage access, and browser terminal access.
- Simplified Start Here so new readers get one primary first path before maintainer-oriented references.
- Moved architecture-heavy links into a platform maintainer section.
- Added first-use links for ElasticBLAST, ElasticBLAST on Azure, Azure, Kubernetes, Azure Container Apps, Redis, Azure Storage, and managed identity, following the project documentation terminology-link guidance.

## API/IaC Diff Summary

- No API or infrastructure changes.
- Documentation-only copy and interaction update on the MkDocs home page and shared documentation assets.

## Validation Evidence

- `uv run mkdocs build` passed.
- Browser check confirmed the Overview page renders the `Cluster plane`, `Resource plane`, and `Live Monitoring` tabs and loads `dashboard1.png`, `dashboard2.png`, and `dashboard3.png`.
- Browser interaction check confirmed the Dashboard screenshots open in a full-viewport image viewer without page/modal scrolling and close with `Escape`.
- Browser layout check confirmed the image viewer overlay covers the full viewport, uses an opaque backdrop, locks both `html` and `body` scrolling, and fits the screenshot inside the modal without scrollbars.