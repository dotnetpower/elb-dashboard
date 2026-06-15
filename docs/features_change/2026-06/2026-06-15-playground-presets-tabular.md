---
title: Service Bus Playground — example presets and tabular multi-token mode
description: The Service Bus Playground gains an Example preset selector (16S XML, core_nt XML, core_nt tabular outfmt 7, core_nt multi-token) and an Output format toggle that switches the request body between the XML-locked options shape and the free-form blast_options shape, so the now-supported multi-token /v1/jobs route can be driven from the browser.
tags:
  - blast
  - operate
---

# 2026-06-15 — Playground presets and tabular multi-token mode

## Motivation

The Service Bus Playground only exercised the XML-locked submit path
(`options.outfmt` fixed to `5`) from a single hardcoded FASTA. It could not
drive the now-supported multi-token `/v1/jobs` route, and gave no curated
starting point for a realistic search.

## User-facing change

The Playground producer pane now offers:

- An **Example** selector with four curated presets that fill the whole form
  (FASTA + database + options), mirroring the API Reference spec:
  - `16S rRNA · XML (fast)` — lightweight smoke against `16S_ribosomal_RNA`.
  - `Monkeypox → core_nt · XML` — Web BLAST-equivalent core_nt, BLAST XML.
  - `Monkeypox → core_nt · Tabular (outfmt 7)` — tabular with comment lines.
  - `Monkeypox → core_nt · Multi-token (7 std staxids …)` — extended columns.
- An **Output format** toggle (XML · outfmt 5 vs Tabular · multi-token). XML mode
  keeps the `options` (`ExternalBlastSubmitRequest`) shape; Tabular mode emits the
  free-form `blast_options` (`ExternalBlastV1Request`) shape with editable
  `outfmt`, raw `extra` CLI flags, and `resource_profile`.

The sample-code pane serializes whichever body shape is active, so the copied
Python / dashboard-API snippets match the message the consumer will route.

## API / IaC diff summary

- `web/src/api/settings.ts` — `ServiceBusSendRequest` gains optional
  `blast_options` (`{ evalue, max_target_seqs, outfmt, extra }`) and
  `resource_profile`, mirroring the existing send-route contract. No backend
  change: the route already branched on `blast_options` via `_validate_send_body`.
- `web/src/pages/ServiceBusPlayground.tsx` — preset table, mode toggle,
  conditional tabular fields, and a `buildBody` that emits the matching shape.

## Validation evidence

- `cd web && npm run build` clean; `npm test -- --run` → 898 passed.
- Live end-to-end (deployed revision `0000525`): a Playground tabular send
  (`outfmt "7 std staxids sstrand qseq sseq"`, `16S_ribosomal_RNA`) was enqueued,
  drained to the sibling `/v1/jobs`, and completed. The result rendered as tabular
  with multi-token columns parsed — 50 hits with `sseqid` / `staxids` (e.g.
  `2488306`, `585054`) / `evalue` / `bitscore` / `pident`.
- XML-mode body validated (`status: valid`); an invalid non-`std`-leading tabular
  layout (`outfmt "6 staxids"`) was rejected with `400` by the submit-time
  shard-merge guard.
