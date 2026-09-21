---
title: Keep database update spinners visibly active
description: Database download and update rows retain a slow visible spinner when the operating system requests reduced motion.
tags:
  - blast
  - ui
  - operate
---

# Keep database update spinners visibly active

## Motivation

The BLAST database manager used the shared `spin` class for active download and
update indicators. The global reduced-motion rule disabled that animation, so an
in-progress database update could display a frozen circular icon.

## User-Facing Change

The database row's leading transfer indicator, inline `Updating` badge, and
right-side update status indicator now use the essential progress-spinner
variant. Under `prefers-reduced-motion: reduce`, they rotate at the slower
two-second cadence instead of appearing stopped.

## API / IaC Diff Summary

No API, task, Azure resource, or data contract changed. The update is limited to
the database row's progress icons and the shared spinner comment.

## Validation Evidence

- Focused `BlastDbRow` tests passed: 11.
- Full frontend suite passed: 1,049 tests across 120 files.
- ESLint and the production Vite build passed.
- Browser inspection with reduced motion enabled reported `animation-name: spin`,
  `animation-duration: 2s`, `playState: running`, and a changing transform
  matrix across frames.
