---
title: Refresh the published documentation site
description: Reconcile all 63 navigated GitHub Pages documents, release indexes, source links, screenshots, and the static UI preview with the current control-plane implementation.
tags:
  - contributor
  - architecture
  - ui
  - release
---

# Refresh the published documentation site

## Motivation

The published site had accumulated drift after several months of backend,
identity, queue, terminal, UI, and OpenAPI changes. High-risk examples included
incorrect feature-gate defaults, subscription-wide RBAC recovery advice,
outdated terminal isolation, stale OpenAPI image tags, broken repository source
links, and screenshots carrying the former **Recent searches** UI.

## User-Facing Change

- Audited every page in the 63-entry MkDocs navigation and aligned current
  architecture, operation, user-guide, contributor, and crawler-index content.
- Documented all behavior-gate environment variables discovered in production
  source, including active shared defaults, process fallbacks, and kill
  switches.
- Corrected the managed-identity scope model, browser/M2M authentication,
  terminal per-operator credential isolation, Service Bus worker defaults,
  submit coordination precedence, Dashboard bands, and current flat UI theme.
- Replaced the aspirational Storage proxy description with the shipped
  contract: inline query JSON staging, SDK-managed result streaming, eight
  default download permits, Storage-account cross-checking, and explicit
  disclosure that Range requests and multipart/block streaming uploads are not
  implemented.
- Standardized the current **BLAST Jobs** product label and replaced six primary
  screenshots plus the ACR build screenshot with sanitized fixture-backed
  captures. The stale live-terminal screenshot is no longer published.
- Regenerated tagged and Unreleased pages from their exact git trees. Deleted
  change notes no longer produce broken release links, and the v0.1.0 baseline
  now includes all 361 notes present at that tag.
- Hardened release publishing so frontmatter titles win over fenced code
  comments, generated GitHub Release bodies omit page frontmatter, and the
  published v0.3.0 index no longer labels a note as `OLD (lines 304-308 before)`.
- Added a MkDocs hook that converts links to repository files outside `docs/`
  into working GitHub source URLs on the published site.
- Repaired the static UI preview's caller-permissions fixture, updated its
  OpenAPI image to 4.61, and replaced the private example service address with
  the RFC 5737 TEST-NET address `192.0.2.10`.

## API / IaC Diff Summary

- No runtime API, Azure role assignment, network policy, message schema, or IaC
  resource changed.
- Documentation tooling changed only in the release-note renderer and MkDocs
  repository-link hook. Frontend runtime behavior is unchanged; only the
  documentation preview fixture and its regression test changed.

## Validation Evidence

- Frontmatter guard passed for all 63 navigated pages, and strict MkDocs builds
  completed successfully.
- Source-relative validation resolved 1,695 links/images. Rendered-site
  validation resolved all 4,724 internal links and all 3,776 local heading
  fragments across the 63 navigated pages.
- Focused tests passed for release rendering/body handling (10), repository-link
  rewriting (1), and documentation-preview fixture contracts (2).
- The full backend suite passed with 5,949 tests and 4 fixture-dependent skips;
  the full frontend suite passed 1,043 tests across 119 files. Ruff, the mypy
  debt ratchet, ESLint, and generated OpenAPI contract/type checks passed.
- Frontend production and documentation-preview builds completed successfully.
- Browser smoke covered five published documentation pages, Dashboard, New
  Search, BLAST Jobs, Results, and API Reference at 1280 x 900 plus Dashboard
  at 390 x 844 with zero console errors, zero ErrorBoundary screens, and no
  horizontal overflow.
- Six primary screenshots decoded at 1280 x 900 or 390 x 844; the ACR detail
  screenshot decoded separately and visibly carries the pinned OpenAPI 4.61
  tag. New Search shows 5/5 readiness and API Reference uses `192.0.2.10`.
- The regenerated architecture export decoded as a 7680 x 3482 RGB PNG with
  symmetric content margins and unclipped SVG-native labels.