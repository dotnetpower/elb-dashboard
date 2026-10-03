---
title: Restore loading icon motion
description: Active loading and refresh icons now rotate consistently, including a slower functional animation when reduced motion is requested.
tags:
  - ui
  - operate
---

# Restore loading icon motion

## Motivation

Several active operations rendered a static loading or refresh icon even though their buttons were disabled and the accompanying copy said Loading, Checking, or Refreshing. The shared browser used for validation also requested [`prefers-reduced-motion`](https://developer.mozilla.org/docs/Web/CSS/@media/prefers-reduced-motion), and the global motion guard collapsed ordinary spinner animations to a single frame. This made most loading indicators appear frozen.

## User-Facing Change

All `Loader2` progress icons and refresh icons tied to an active request now use the shared rotation treatment. Normal motion rotates once per second. Reduced-motion mode keeps this functional state cue moving at a slower two-second rate instead of showing a static frame.

Idle refresh, reset, retry, update, and countdown icons remain static until an operation is active.

## API / IaC Diff Summary

- Added shared spin classes to previously static loading states in Settings and Service Bus controls.
- Bound active refresh icons in API status, BLAST Jobs, Service Bus settings, and observed completion views to their request state.
- Removed a component-local duplicate `@keyframes spin`; the global stylesheet is the only animation definition.
- Added a source-contract test that scans all TSX files for static `Loader2` usage, required active refresh bindings, and local spin keyframes.
- No API, persistence, authentication, RBAC, or IaC changed.

## Validation Evidence

- Loading icon motion contract passed: 7 tests.
- Full frontend suite passed: 1,056 tests across 121 files.
- ESLint, production build, and strict MkDocs build passed.
- Browser validation with reduced motion enabled reported `animation-duration: 2s`, `animation-iteration-count: infinite`, and changing transforms after 250-300 ms at 1204 x 761 and 390 x 844.
