---
title: AKS provision modal closes immediately on task enqueue
description: The Create AKS Cluster modal now closes as soon as the provision task is accepted, with the in-card progress banner and error card serving as the live-progress and retry safety net.
tags:
  - ui
  - infra
---

# AKS provision modal closes immediately on task enqueue

## Motivation

The "Create AKS Cluster" modal previously stayed open until ARM published the
cluster's `cluster_state` (Creating/Updating/Succeeded) — roughly 70 s after the
user clicked **Create**, while the worker submitted the `arm_create_or_update`
call. During that window the modal showed a live "Step 3/5 · Submitting cluster
create to Azure" panel that exactly duplicated the in-card progress banner. The
user found it more natural to dismiss the modal at that 3/5 moment rather than
keep staring at a modal that mirrors the card.

The original reason the modal stayed open (the 2026-05-24 AKS Provisioning UX
overhaul) was to avoid losing the user's form inputs if ARM rejected the create
~70 s later with a quota/SKU error. That safety net now lives on the card, so
the modal no longer needs to stay open to provide it.

## User-facing change

- Clicking **Create** shows the modal's "Creating…" button for the brief POST
  round-trip, then the modal **closes immediately** once the provision task is
  accepted (a `task_id` is returned).
- The in-card **ProvisioningBanner** takes over and shows the same live
  Step N/5 / phase / elapsed progress the modal used to show.
- If the task later fails or ARM rejects the create, the in-card
  **ProvisionErrorCard** surfaces the error inline with **Dismiss** and
  **Edit & retry**. Because the provision form state lives in the parent hook
  (`useClusterProvisioning`), not in the modal, **Retry reopens the modal with
  every input preserved** — no input loss, which was the whole point of the
  original "keep the modal open" behaviour.

## Implementation

Single behavioural change plus comment updates in
`web/src/components/cards/ClusterCard/useClusterProvisioning.ts`:

- The two-stage close effect's Stage-1 trigger changed from
  `armAccepted` (waiting for `taskProgress.cluster_state`) to `taskId` present
  (the enqueue POST returned). `taskId` was added to the effect dependency
  array. Stage-2 "done" detection is unchanged.
- The stale comment blocks in `handleProvision` (the "**Do not** close the modal
  here…" block and the catch "Modal intentionally stays open…" comment) were
  rewritten to describe the new immediate-close flow and the card-side safety
  net.

No change to `ProvisionModal.tsx`, `ClusterCard.tsx`, or any backend/IaC code.
The card already rendered `ProvisioningBanner` while `provStatus === "creating"`
and `ProvisionErrorCard` while `provError && !showProvision`, so the safety net
was already in place — only the close trigger moved earlier.

## Validation

- `cd web && npm run build` — clean.
- `cd web && npm test -- --run` — 58 files / 454 tests passed.
- `web/src/components/cards/ClusterCard/useClusterProvisioning.ts`,
  `ProvisionModal.tsx`, `SettingsPanel.tsx` — no TypeScript errors.
