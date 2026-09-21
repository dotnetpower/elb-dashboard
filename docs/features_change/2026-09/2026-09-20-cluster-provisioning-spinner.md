---
title: Keep cluster provisioning status visibly active
description: The cluster provisioning spinner remains visibly active at a reduced speed when the operating system requests reduced motion.
tags:
  - ui
  - operate
---

# Keep cluster provisioning status visibly active

## Motivation

The dashboard's global reduced-motion rule disabled every animation, including
the spinner that communicates an active AKS provisioning operation. On systems
with reduced motion enabled, the icon looked frozen even though provisioning was
still running.

## User-Facing Change

The main cluster provisioning spinner and in-progress pool spinner now continue
rotating at a slower two-second cadence under `prefers-reduced-motion: reduce`.
Other decorative motion remains disabled.

## API / IaC Diff Summary

No API, task, Azure resource, or request/response contract changed. The change is
limited to the provisioning banner's icon class and the shared frontend theme.

## Validation Evidence

- Full frontend suite passed: 1,048 tests across 120 files.
- ESLint and the production Vite build passed.
- Browser inspection with reduced motion enabled reported `animation-name: spin`,
  `animation-duration: 2s`, and a changing transform matrix across frames.
