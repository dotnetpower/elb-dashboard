# User Guide

This guide explains how to operate the ElasticBLAST Control Plane from the browser. It is organized around the main product surfaces that researchers use during a BLAST workflow.

## Workflow

1. Open the [Dashboard](dashboard.md) to confirm the Azure resources are ready.
2. Create a search in [New Search](new-search.md).
3. Track progress in [Jobs](jobs.md).
4. Review and download outputs in [Results](results.md).
5. Use the [Browser Terminal](terminal.md) only when command-line inspection is needed.
6. Use the [API Reference](api-reference.md) for operator and integration checks.

Screenshots are captured from a controlled demo environment. The capture process and redaction rules are documented in the [Screenshot Workflow](../contributor-guide/screenshot-workflow.md).# User Guide

This section is prepared for the screenshot-based product guide. The pages below use the current application route map and reserve stable screenshot targets so the guide can be completed after the demo environment is in a clean, representative state.

## Planned Pages

| Page | App route | Primary screenshot target |
| --- | --- | --- |
| [Dashboard](dashboard.md) | `/` | `docs/images/screenshots/dashboard-overview.png` |
| [New Search](new-search.md) | `/blast/submit` | `docs/images/screenshots/new-search-form.png` |
| [Jobs](jobs.md) | `/blast/jobs` | `docs/images/screenshots/jobs-list.png` |
| [Results](results.md) | `/blast/jobs/{jobId}` | `docs/images/screenshots/results-overview.png` |
| [API Reference](api-reference.md) | `/docs` | `docs/images/screenshots/api-reference.png` |
| [Terminal](terminal.md) | `/terminal` | `docs/images/screenshots/terminal-session.png` |

Use the [Screenshot Workflow](../documentation/screenshot-workflow.md) before adding images to these pages.