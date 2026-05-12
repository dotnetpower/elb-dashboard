# Getting Started guide modal on first workspace open

**Date**: 2026-05-12
**Scope**: `web/src/components/GettingStartedGuide.tsx` (new),
`web/src/pages/Dashboard.tsx`

## Motivation

A workspace that has just been registered (via Setup Wizard or
auto-discovery) almost always still needs three things before
ElasticBLAST can run: container images built, AKS cluster created,
and a Remote Terminal provisioned. New operators consistently
asked "where do I start?" after the wizard closed and the Dashboard
came up empty. The README/docs answer existed but lived outside
the app.

## User-facing change

- After a workspace is selected (saved config has subscription,
  workload RG, ACR, storage), the Dashboard auto-shows a modal
  **Getting Started** guide listing the next steps:
  1. Build container images (links to the ACR card).
  2. Create an AKS cluster (links to the AKS card).
  3. Provision a Remote Terminal (links to the Terminal page).
  4. Download a BLAST database (links to the Storage card),
     including a short curated list of recommended databases with
     sizes.
- Each step shows a check mark when satisfied (probed live via the
  existing `aks`, `acr`, and `terminal` monitoring endpoints).
- A progress bar at the top reflects completion (`X / 4 done`).
- The modal is dismissible. Dismissal is remembered for the
  current browser session via `sessionStorage`
  (`elb-getting-started-dismissed=true`) so it does not reappear on
  every navigation. A new session re-evaluates state and shows the
  guide again only if the workspace still needs setup.

## API / IaC diff summary

`web/src/components/GettingStartedGuide.tsx` (new):
- Pure presentational component. Takes `hasCluster`, `hasImages`,
  `hasTerminal`, `clusterRunning`, `acrName`, `onDismiss`. Renders a
  modal overlay with the four steps.
- No new endpoints — relies on the parent to compute readiness.

`web/src/pages/Dashboard.tsx`:
- Three new `useQuery` calls (AKS, ACR, terminal) gated on
  `hasConfig && !gettingStartedDismissed`. 60 s `staleTime`, single
  retry, so the probes are cheap and never blow up the dashboard.
- `needsSetup` boolean: any of cluster / images / terminal missing.
- `useEffect` auto-opens the guide when probes have all returned
  and `needsSetup` is true.
- `gettingStartedDismissed` initialised from `sessionStorage` and
  written back on dismiss.

## Validation evidence

- `npx tsc --noEmit` (web) → clean.
- `npx vite build --mode production` → succeeded.
- SPA already deployed. The modal rendered in the staging
  workspace once the saved config was complete and the cluster /
  images / terminal probes returned "missing".
- Pending: visual review against multiple workspace states
  (everything done, partial, none).
