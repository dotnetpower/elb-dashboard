---
title: Inline download progress for result files
description: Result-file downloads now stream and show a live percent/bytes indicator instead of a bare spinner.
tags:
  - blast
  - ui
---

# Inline download progress for result files (#24)

## Motivation

Downloading a result file showed only a spinner — no sense of how much was left,
which is rough for large (multi-MB) `.out` / XML results streamed through the api
sidecar.

## User-facing change

- The download button now shows live progress while a file downloads: a percent
  when the server sends `Content-Length`, otherwise the received byte count
  (e.g. `42%` or `2.3 MB`). The blob is still fully materialised, so the actual
  download behaviour is unchanged — only the feedback is richer.

## Code change summary

- [web/src/api/blast.ts](../../../web/src/api/blast.ts): `downloadResultFile`
  gained an optional `onProgress(received, total)` callback and streams the
  response body via a `ReadableStream` reader (falls back to a one-shot
  `response.blob()` when the body stream or callback is absent).
- [web/src/hooks/useBlastResultActions.ts](../../../web/src/hooks/useBlastResultActions.ts):
  added `downloadProgress` state (exported `BlastDownloadProgress` type), wired
  the callback, cleared it in `finally`.
- Threaded `downloadProgress` through
  [ResultsCard](../../../web/src/pages/blastResults/ResultsCard.tsx) →
  [ResultsBody](../../../web/src/pages/blastResults/ResultsBody.tsx) →
  [BlastResultsTable](../../../web/src/pages/blastResults/BlastResultsTable.tsx)
  (`ResultsFileTable` / `ArtifactDetails`) → `BlastResultRow`, which renders the
  percent/bytes label next to the spinner.

## Validation evidence

- `cd web && npm run build` → clean.
- `cd web && npm test -- --run` → 929 passed (full suite).
- `npx eslint` on the five changed files → clean.

No backend / API / IaC changes (the streaming endpoint already exists).
