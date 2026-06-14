---
title: Message lifecycle card + default-OFF Service Bus ingress flags wired in Bicep
description: Run details now renders the BLAST message lifecycle trace, and the Tier 2/3 Service Bus ingress flags are declared default-OFF in the Container App template.
tags:
  - blast
  - ui
  - infra
---

# 2026-06-14 — Message lifecycle UI + Bicep flag wiring (#36 finish)

Completes the Service Bus ingress unification ([#36](https://github.com/dotnetpower/elb-dashboard/issues/36)):
surfaces the message lifecycle trace in the dashboard UI (the "track the whole
process" requirement) and declares the Tier 2/3 runtime flags default-OFF in the
Container App template so they survive every redeploy.

## User-facing change

* **Run details → "Message lifecycle" card.** The job detail Run details tab now
  renders the BLAST message lifecycle for Service-Bus / OpenAPI-plane jobs:
  the ordered stages (enqueued → received → row_created → routed → submitted →
  running → succeeded|failed → result delivered) with each hop's timestamp, plus
  the derived **Queue dwell / Submit latency / End-to-end** metrics. A
  dashboard-Celery job has no message lifecycle, so the card renders nothing for
  it (no empty shell). The card owns its own `history: true` fetch so the page's
  main poll stays cheap.
* **Tier 2/3 flags are now declared default-OFF in infra.** `ENABLE_SB_SUBMIT_INGRESS`
  (api) and `SERVICEBUS_RESIDENT_CONSUMER` (worker) are added to
  `infra/control-plane-env.json` and the api/worker env arrays in
  `containerAppControl.bicep`, both `false` (charter §12a Rule 4). They
  propagate on both deploy paths (full azd and the GHA `--set-env-vars` PATCH via
  `quick-deploy.sh control_plane_env_pairs`), so flipping one to `true` survives
  every redeploy.

## API / IaC diff summary

* `web/src/api/blast.types.ts` — `BlastJobSummary.message_trace` +
  `BlastMessageTrace` / `BlastMessageTraceStage` types.
* `web/src/pages/blastResults/MessageTraceCard.tsx` (new) — the card.
* `web/src/pages/blastResults/messageTraceModel.ts` (new) — pure view-model
  helpers (`fmtTraceMs`, `visibleTraceStages`, stage labels/order).
* `web/src/pages/BlastResults.tsx` — renders the card on the Run details tab.
* `infra/control-plane-env.json` + `infra/modules/containerAppControl.bicep` —
  `ENABLE_SB_SUBMIT_INGRESS` (api) and `SERVICEBUS_RESIDENT_CONSUMER` (worker),
  default `false`.

## Validation evidence

* Backend: `uv run pytest -q api/tests` → **3599 passed, 3 skipped** (no backend
  change in this slice; confirms no drift).
* Frontend: `npm run test -- --run` → **894 passed** (incl. new
  `messageTraceModel.test.ts`, 10 cases); `npm run build` succeeds; eslint clean
  on changed files.
* Infra: `az bicep build --file infra/modules/containerAppControl.bicep` exits 0.
