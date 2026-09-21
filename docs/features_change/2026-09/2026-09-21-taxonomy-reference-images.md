---
title: Restore taxonomy reference images
description: Taxonomy details accept Wikimedia's current thumbnail host, keep the browser CSP aligned, and avoid caching transient missing-image results for a full day.
tags:
  - blast
  - ui
  - security
---

# Restore taxonomy reference images

## Motivation

The [Wikipedia REST API](https://www.mediawiki.org/wiki/API:REST_API) moved
thumbnail delivery for records such as `Homo sapiens` from
`upload.wikimedia.org` to `thumb.wikimedia.org`. The backend accepted only the
old host, discarded the valid thumbnail URL, and cached that empty result for 24
hours. The frontend security policy also omitted both Wikimedia image origins.

## User-Facing Change

Taxonomy details once again show available reference images. Both current and
legacy [Wikimedia](https://www.wikimedia.org/) image hosts are supported. A
missing or transiently unavailable image is retried after five minutes instead
of remaining unavailable for a full day in either the backend or browser query
cache.

## API / IaC Diff Summary

- Thumbnail URLs require HTTPS, an exact `thumb.wikimedia.org` or
  `upload.wikimedia.org` hostname, no credentials, and the default HTTPS port.
- Successful image results retain the 24-hour cache. Empty results use a
  five-minute negative cache.
- The nginx and legacy static-hosting CSPs add only those two exact image
  origins. No wildcard image source was added.
- The taxonomy image response shape and route are unchanged. No Azure resource
  or IaC contract changed.

## Validation Evidence

- Taxonomy image tests passed: 20.
- Full backend suite passed: 6,134 with 4 fixture-dependent skips.
- Full frontend suite passed: 1,049 tests across 120 files.
- Ruff, the mypy debt ratchet, ESLint, the 242-operation OpenAPI contract,
  generated TypeScript types, the production Vite build, and strict MkDocs
  build passed.
- The local API returned the live Wikimedia thumbnail URL for `Homo sapiens`.
- Browser image decoding succeeded at 330 x 552 pixels.
