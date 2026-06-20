---
title: Service Bus Playground — editable, auto-validated request body
description: The Playground's Sample code pane now has an editable request-body JSON editor with live JSON validation; Validate / Send moved below it and submit the exact edited body.
tags:
  - ui
  - blast
---

# Service Bus Playground — editable, auto-validated request body

## Motivation

The Service Bus Playground (`/blast/playground`) let you compose a BLAST request
in the producer form, but the request was only ever sent as the form-derived
body — the **Sample code** pane was read-only, and the **Validate** / **Send**
buttons lived in the form pane. There was no way to hand-edit the exact JSON
that gets enqueued.

## User-facing change

* The **② Sample code** pane now leads with an **editable Request body (JSON)**
  textarea. It mirrors the producer form until you edit it by hand; after that
  it keeps your edit (a **Reset to form** button re-syncs from the form).
* **Auto-validation**: the body is `JSON.parse`-checked on every keystroke, with
  a live `Valid JSON · ready to send` / `Invalid JSON — <reason>` status line and
  a red editor border while invalid.
* **Validate** and **Send** moved to the **bottom of the Sample code pane** and
  now submit the **exact edited body** (Validate adds `dry_run`; Send strips it).
  Both are disabled while the JSON is invalid (and Send also while the Service
  Bus integration is off).
* The read-only Python / Dashboard-API reference snippets below the editor now
  reflect the edited body, so copy-paste stays in sync with what you send.

## API/IaC diff summary

* [web/src/pages/ServiceBusPlayground.tsx](../../../web/src/pages/ServiceBusPlayground.tsx)
  — added `bodyDraft` / `bodyDirty` state + form→editor sync, a live JSON
  parse (`bodyValue` / `bodyError`), routed `sendMutation` through the edited
  body, and moved the Validate / Send / result block into Pane 2 under the
  editor. No API change — the request shape sent to
  `POST /api/settings/service-bus/send` is unchanged.

## Validation evidence

* `cd web && npm run build` → built clean (tsc + vite, no type errors).
* `npx eslint src/pages/ServiceBusPlayground.tsx` → clean.
* The page is gated behind the Settings → Preview "Service Bus Playground"
  toggle; live screenshot deferred until the next frontend deploy.
