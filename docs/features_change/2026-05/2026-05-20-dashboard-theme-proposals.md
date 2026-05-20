# Dashboard Theme Proposals

## Motivation

Earlier proposals tried to surface every Dashboard capability at once (AKS, Storage, ACR, databases, sidecars, terminal, guardrails, real-time monitoring, ...) and ended up dense and noisy. The user redirected scope to the three essential questions an operator actually asks when they open the Dashboard:

1. Is the server healthy?
2. Are jobs healthy?
3. What is the traffic right now?

This change replaces the multi-card proposals with three minimal directions that each answer only those three questions.

## User-facing change

- Rewrote the standalone static mockup at `web/public/dashboard-new-search-theme-proposals.html` so every proposal renders only Server health, Jobs health, and Current traffic.
- Proposal A - Stacked verdicts: 2x2 grid. Top row stacks the Server-health verdict and the Jobs-health verdict side by side (each with a one-line answer plus 3 supporting metrics). Bottom row stacks the Active jobs list (left) and the Current traffic chart (right).
- Proposal B - Single hero verdict: full-width hero panel combining the Server and Jobs verdicts into one banner plus a 3-metric hero strip. Below it, Active jobs (left) and Current traffic chart (right).
- Proposal C - Three answers side by side: three equal columns, one question per column. Column 1 lists the 6 server-component checks, column 2 shows the Jobs summary + active jobs, column 3 shows the traffic chart plus peak / avg / CPU avg metrics.
- All three proposals keep the in-place Light/Dark theme toggle and a primary `New Search` CTA.
- Removed the previous real-time monitoring multi-card grid, AKS/Storage/ACR/database panels, guardrail strips, jump bar, and left navigation remnants.

## API/IaC diff summary

No API or IaC changes. Frontend mockup-only.

## Validation evidence

- `npm run build` in `web/` completed successfully on 2026-05-20 (`vite v5.4.21 ... built in 7.77s`).
- `npx prettier --write public/dashboard-new-search-theme-proposals.html` reformatted the file cleanly.
- Browser verification at `http://127.0.0.1:8090/dashboard-new-search-theme-proposals.html` (viewport 1305 x 669) confirmed:
  - Three proposal screens (`.screen` x 3), six theme toggle buttons (`.theme-toggle button` x 6).
  - Three traffic charts (`.chart svg` x 3), five verdict blocks (A: 2 + B: 1 + C: 2 = 5), nine active job rows (`.job-row` x 3 per proposal), six server-component check rows in Proposal C (`.check-row` x 6), four panels in Proposal A.
  - Proposal A panel measures 1266 x 678 px (within ~9 px of the 669 px viewport), Proposal B and C panels measure 1266 x 645 px (matching the viewport min-height).
  - Light/Dark toggle confirmed: clicking the Dark button on Proposal B switched `#proposal-b` to `.is-dark`.
  - No leftover real-time monitoring labels or navigation: `Live monitoring`, `monitor sweep`, `preflight`, `.jump-bar`, `.left-nav` all absent.
- Screenshot of Proposal A confirms the 2x2 layout renders the four panels (Server health verdict, Jobs health verdict, Active jobs, Current traffic) cleanly in one viewport.
