---
title: About build identity alignment
description: Keep the Help About panel aligned with the running SPA build stamp, branding, and Container Apps architecture.
tags: [ui, release]
---

# About build identity alignment

## Motivation

The Help About panel still displayed the original `0.1.0`, Azure Functions, and Python 3.11 prototype details even though the top bar reported the current build and the control plane now runs on Azure Container Apps. Its activity-wave icon also differed from the DNA brand mark in the application header.

## User-facing change

- The About version now uses the same release, build-number, and commit inputs as the top bar and Settings footer.
- Runtime and backend labels now identify Azure Container Apps, FastAPI, and Python 3.12.
- Technology badges now reflect the FastAPI, Celery, Redis, Azure Storage, and managed identity architecture.
- The About brand mark now uses the same DNA glyph, stroke weight, and gradient as the application header.

## API and infrastructure summary

No API, authentication, storage, role assignment, network, or infrastructure contract changed.

## Validation

- `npm test -- --run`: 109 files and 985 tests passed.
- `npm run lint`: passed.
- `npm run build`: passed.
- Local browser verification confirmed that the top bar and About panel both rendered `v0.3.30 · f4b58c1b` and each contained one Lucide DNA brand icon.
