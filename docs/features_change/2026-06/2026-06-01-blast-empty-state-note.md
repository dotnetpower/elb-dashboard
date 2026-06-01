---
title: BLAST jobs empty-state "no shared queue" note
description: Empty-state copy clarifying that searches run on the user's own AKS cluster and storage with no shared NCBI rate limit.
tags:
  - blast
  - ui
---

# BLAST jobs empty-state "no shared queue" note

## Motivation

Users coming from NCBI Web BLAST expect a shared submission queue with rate
limits and wait lines. ElasticBLAST on Azure runs each search on the user's own
AKS cluster and storage, so there is no public queue. Saying so on the empty
state sets the right mental model before the first search.

## User-facing change

The BLAST jobs empty state now shows a muted note under "No BLAST searches yet.":

> Your searches run on your own AKS cluster and storage — never on a shared NCBI
> queue, so there is no public rate limit or wait line.

## API / IaC diff summary

- `web/src/pages/BlastJobs/JobsEmptyState.tsx` — one added paragraph. No backend
  or infra change.

## Validation evidence

- `cd web && npm run build` — clean.
- `cd web && npm test -- --run` — 454 passed.
