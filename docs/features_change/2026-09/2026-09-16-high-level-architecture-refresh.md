---
title: Refresh the High Level Architecture diagram
description: Update the published architecture map for optional Service Bus ingress, isolated Celery workers, terminal exec, private Storage roles, and AKS workload identity.
tags:
  - architecture
  - contributor
---

# Refresh the High Level Architecture diagram

## Motivation

The [GitHub Pages](https://docs.github.com/pages) architecture map still showed
the original browser-to-Container-App flow. It did not include the optional
[Azure Service Bus](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview)
integration, the terminal exec channel, isolated Celery worker pools, or the
separate platform and workload Storage responsibilities now used by the
deployed control plane.

## User-Facing Change

- Rebuilt the [Mermaid](https://mermaid.js.org/) diagram around the current
  entry, control-plane, optional messaging, private data, and AKS workload
  boundaries.
- Distinguished the in-revision Redis Celery broker from the default-OFF
  Service Bus integration for request ingress and completion events.
- Added the browser terminal and authenticated exec-server paths, worker queue
  isolation, platform versus workload Storage, and the AKS OpenAPI federated
  identity.
- Updated the surrounding flow descriptions so they use the same current
  contracts as the diagram.
- Added a full-resolution 7680 x 3482 PNG export and a download link below the
  live Mermaid diagram.

## API / IaC Diff Summary

- Documentation only; no API, message schema, environment default, identity,
  network policy, or infrastructure resource changed.

## Validation Evidence

- `uv run python scripts/docs/check_frontmatter.py` passed for all 63 navigated
  pages.
- `DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --strict` completed
  successfully.
- Browser rendering produced one Mermaid SVG and zero parse-error elements.
  The compact graph's source aspect ratio is 2.18, down from 4.0 for the first
  draft, with no page-level horizontal overflow at 1440 px or 390 px viewports.
- Browser checks confirmed the Service Bus, worker isolation, terminal exec,
  and private-access labels are present. Fullscreen mode rendered the SVG at
  1400 x 643 px and closed with `Escape`.
- The exported PNG decoded successfully as a 7680 x 3482 RGB image. Its
  non-white content has symmetric 49 px horizontal and 50 px vertical margins.
  The Mermaid export uses SVG-native labels; a whole-image review plus DOM
  checks confirmed all 55 text nodes, including the shared-token M2M path,
  render without clipping or document chrome.