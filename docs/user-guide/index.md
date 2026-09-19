---
title: User Guide
description: End-to-end browser workflow for researchers using ElasticBLAST Control Plane — Dashboard, New Search, Jobs, Results, API Reference, and the in-browser terminal.
social:
  cards_layout_options:
    title: User Guide
    description: End-to-end browser workflow for researchers — Dashboard, New Search, Jobs, Results, and Terminal.
tags:
  - user-guide
---

# User Guide

This guide explains how to operate the ElasticBLAST Control Plane from the browser. It is organised around the main product surfaces researchers use during a BLAST workflow.

!!! tip "TL;DR"

    Researchers move through five surfaces: **Dashboard** (readiness),
    **New Search** (submit), **BLAST Jobs** (track), **Results**
    (inspect + download), and **Terminal** (advanced shell). The
    **API Reference** page is for developers who want to drive the
    backend directly.

## Workflow

1. Open the [Dashboard](dashboard.md) to confirm Azure resources are ready (AKS, Storage, ACR, terminal sidecar, BLAST databases).
2. Create a search in [New Search](new-search.md), starting from the BLAST program and finishing with a command preview.
3. Track progress in [BLAST Jobs](jobs.md) and open a job to see live status.
4. Review and download outputs on the [Results](results.md) page.
5. Use the [Browser Terminal](terminal.md) only when command-line inspection is needed — the in-browser shell runs inside the control-plane environment, no laptop tools required.
6. Use the [API Reference](api-reference.md) when you need to integrate an external client or test a single endpoint.

## Pages at a Glance

| Page | App route | Primary screenshot |
| --- | --- | --- |
| [Dashboard](dashboard.md) | `/` | `docs/images/screenshots/dashboard-overview-desktop.png` |
| [New Search](new-search.md) | `/blast/submit` | `docs/images/screenshots/new-search-desktop.png` |
| [BLAST Options Reference](blast-options.md) | New Search option panels | — |
| [BLAST Jobs](jobs.md) | `/blast/jobs` | `docs/images/screenshots/jobs-desktop.png` |
| [Results](results.md) | `/blast/jobs/{jobId}` | `docs/images/screenshots/results-desktop.png` |
| [Pipeline Export](pipeline-export.md) | Results export menu | — |
| [Web BLAST Parity](web-blast-parity.md) | New Search precise-mode evidence | — |
| [Observability](observability.md) | Dashboard, Live Wall, and Settings | `docs/images/screenshots/observability/sidecar-runtime.png` |
| [Browser Terminal](terminal.md) | `/terminal` | Live sidecar required; not included in the static mock preview |
| [API Reference](api-reference.md) | `/docs` | `docs/images/screenshots/api-reference.png` |
| [In-app Upgrades](upgrades.md) | `/upgrade` and Settings → Updates | — |
| [UI Preview](ui-preview.md) | `/mock-app/` | Static mock build — no live data |

## Try Without An Azure Subscription

If you do not have a deployed control plane yet, open the [UI Preview](ui-preview.md). It runs the same React app with fixture data and lets you click through every page below — Dashboard, New Search, BLAST Jobs, Results, and the API Reference — without provisioning AKS, Storage, or ACR.

## Screenshot Policy

Screenshots in this guide are captured from a controlled demo environment. Capture targets, viewports, and the redaction checklist live in [`docs/screenshot-capture-manifest.json`](../screenshot-capture-manifest.json); the end-to-end capture process is documented in the [Screenshot Workflow](../contributor-guide/screenshot-workflow.md). Refresh an image only when the visible state of that screen changes.
