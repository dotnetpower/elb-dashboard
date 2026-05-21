---
title: UI Preview (User Guide)
description: Static, fixture-backed build of the ElasticBLAST Control Plane SPA embedded in the documentation site — explore the UI without provisioning Azure resources.
---

# UI Preview

The UI Preview is a static, mock-backed build of the real Control Plane React app. It runs inside the documentation site and uses fixture data instead of Azure, AKS, Storage, ACR, Redis, or the FastAPI backend.

Use it when you want to review the product surface from GitHub Pages, share a screen with a researcher, or check the shape of the UI without touching live infrastructure.

## Open The Preview

[Open Mock Control Plane](../../mock-app/){ .md-button .md-button--primary }

## Scenario Links

| Scenario | Link |
| --- | --- |
| Dashboard ready | [Open](../../mock-app/#/?scenario=ready) |
| HTTP Request Inspector | [Open](../../mock-app/#/?scenario=ready&inspector=http) |
| First-run setup required | [Open](../../mock-app/#/?scenario=first-run) |
| AKS stopped | [Open](../../mock-app/#/?scenario=cluster-stopped) |
| Database preparing | [Open](../../mock-app/#/?scenario=db-preparing) |
| New Search | [Open](../../mock-app/#/blast/submit?scenario=ready) |
| Jobs running/completed/failed | [Open](../../mock-app/#/blast/jobs?scenario=ready) |
| Results with BLAST XML | [Open](../../mock-app/#/blast/jobs/bb61858a-8cb6-4590-a2e3-c144662851f7?scenario=ready) |
| API Reference with token | [Open](../../mock-app/#/docs?scenario=ready) |

## Embedded Preview

<iframe
  src="../../mock-app/#/?scenario=ready"
  title="Mock ElasticBLAST Control Plane preview"
  style="width: 100%; min-height: 900px; border: 0; border-radius: 8px; background: #0f172a;"
  loading="lazy"
></iframe>

## Local Build

To regenerate the static preview before a local MkDocs build:

```bash
bash scripts/docs/build-mock-preview.sh
uv run mkdocs build
```

The GitHub Pages workflow runs the same mock build before publishing the documentation site.