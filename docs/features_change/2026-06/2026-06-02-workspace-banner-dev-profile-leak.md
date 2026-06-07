---
title: Workspace banner no longer leaks the az-jungha dev profile hint
description: Rewrite the subscriptions_unavailable workspace-diagnostics banner so it stops telling deployed users to run `az login` / pick the personal `az-jungha` az profile, and instead describes the managed-identity Reader requirement.
tags:
  - ui
  - auth
---

# Workspace banner: drop `az-jungha` dev hint, describe managed-identity Reader

## Motivation

When the dashboard could not list any Azure subscriptions it showed a
"Sign in to Azure to load workspace data" banner whose body read:

> The dashboard could not list any Azure subscriptions for your current
> credential. Run `az login --tenant <your-tenant>` in a terminal (or pick a
> different az profile such as `az-jungha`), then click Reset workspace…

Two problems:

1. **`az-jungha` is a personal local az-profile alias** that leaked into the
   shipped production UI. It is meaningless to any real user.
2. The advice is **wrong for the deployed app**. The Container App backend lists
   subscriptions with its **managed identity** (`id-elb-dashboard-*`), not the
   signed-in user's `az login`. Telling a deployed user to run `az login` cannot
   fix an empty subscription list — the actual requirement is the managed
   identity holding the `Reader` role at the subscription scope (or waiting for a
   freshly granted assignment to propagate).

Diagnosed live: the api sidecar's managed identity already has subscription-scope
`Reader`, and `GET /api/arm/subscriptions` currently returns `200 OK`, so the
banner in the report was a transient/cold-start (or just-after-sign-in) empty
list — exactly the case the corrected wording now explains.

## User-facing change

The `subscriptions_unavailable` banner body and the two inline
`WorkspaceDiagnosticsBanner` descriptions now:

- never name a personal az profile (`az-jungha` removed);
- explain the deployed managed-identity Reader requirement and propagation delay
  first, then the local `az login` case;
- still point at the **Reset workspace** retry action.

No backend or API change. The short banner title ("Sign in to Azure to load
workspace data") is unchanged.

## Diff summary

- `web/src/utils/monitorDegraded.ts`: rewrote `bannerBody` for
  `subscriptions_unavailable`.
- `web/src/components/WorkspaceDiagnosticsBanner.tsx`: rewrote the two inline
  `subscriptions_unavailable` `DegradedInfo.description` strings (error case and
  empty-list case) and relabelled the error case from "Sign in to Azure" to
  "Subscriptions unavailable".

## Validation evidence

- `npm test -- --run src/utils/monitorDegraded.test.ts` → 19 passed.
- `npm run build` → built in ~11s, no type errors.
- `grep -r az-jungha web/src` → 0 matches.
