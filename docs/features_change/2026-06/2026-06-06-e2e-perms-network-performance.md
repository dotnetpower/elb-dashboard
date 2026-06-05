# E2E: permissions + network-isolation + performance scenario suites

## Motivation

Follow-up to the restricted-user persona work. The user asked to make
**permissions, network issues, and performance** all fully testable in E2E. The
mock lanes previously covered permissions only (3 cases) and had no coverage for
the network-isolation degraded states or the live performance/metrics surfaces —
both of which are central to the charter (Storage `publicNetworkAccess: Disabled`
posture §9, and warm-cache / node-pressure monitoring).

## What was added

### Fixture: per-endpoint response overrides
`scripts/e2e/fixtures/mockApi.ts` gains `UiMockState.responses` +
`setResponse(key, value)`. The `databases`, `storage`, `topNodes`, `warmup`,
`acr`, and `startStats` routes now serve `state.responses[key] ?? <default>`, so
a scenario can vary just the payload it cares about (call before `page.goto`)
while every existing scenario keeps its default payload. Additive and
backward-compatible — the default branch is the previous inline object verbatim.

### New suite: `network-isolation.ui.spec.ts` (4 cases)
- `public_access_disabled: true` on `/api/blast/databases` → Storage card shows
  the **"access blocked"** pill + "Storage is Private only" tooltip.
- `public_network_access: "Enabled"` on `/api/monitor/storage` → **"Public
  allowed"** incident badge + "Public endpoint is enabled" tooltip.
- Default (Disabled) → steady-state **"Private only"** badge.
- `/api/monitor/acr` degraded `network_blocked` → ACR card surfaces the
  **"Network blocked"** status label.

### New suite: `performance-metrics.ui.spec.ts` (3 cases)
- `top-nodes` busy node (CPU 90% / mem 88% / cache) → expand cluster row → "Open
  cluster detail" → **Node Resources** panel renders the per-node 90% / 88%.
- Same → the **file-cache** overlay legend appears when `cache_ki > 0`.
- `warmup-status` `status: "Loading"` → the cluster detail modal warm-cache
  section names the **core_nt** database being warmed.

### Extended: `restricted-user-events.ui.spec.ts` (+1 case → 5 total)
- Reader persona → the ACR **Build** button is disabled with the "do not have
  permission to build ACR images" tooltip (`can_build_acr` gate), alongside the
  existing Stop/Delete/Run BLAST gating and the full-access control case.

## Validation evidence

- `npx playwright test network-isolation performance-metrics restricted-user-events`
  — **11 passed**.
- `npx playwright test --project ui-mock --project mutation-mock --workers=2` —
  full mock lane green: **25 passed** (14 pre-existing ui-mock + 3 mutation-mock
  + 8 new perms/network/perf cases). NOTE: at the default 6 workers the vite dev
  server intermittently drops module fetches (`net::ERR_NETWORK_CHANGED` on
  `/src/*.ts`) under load, tripping `assertClean`; this is dev-server overload
  noise, not a scenario defect — re-running at `--workers=2` is green. The
  sanctioned `e2e-ui.sh` path serves a built SPA so it is unaffected.
- `npx vitest run src/components/PermissionGate.test.ts src/pages/blastSubmit` —
  189 passed (no unit regression).
- No product code changed — additive test fixtures + two new scenario files +
  one extended scenario.

## How to run (no Azure cost)

```bash
scripts/dev/e2e-ui.sh bypass --headless -- npm --prefix web run e2e:all-safe
# or, manually: vite on :8090 + dev-bypass api on :8085 with
#   CORS_ALLOW_ORIGINS=http://localhost:8090
# then: npx playwright test -c web/playwright.e2e.config.ts --project ui-mock
```

## Not covered here (needs Azure)

The `startStats` override is wired but the cluster-start ETA panel
(`StartEstimatePanel`, measured-vs-default timing text) only renders during a
live start transition, which the mock lane does not drive. Asserting the real
measured-timing copy belongs to the login-mode / `azure-lifecycle` lane and is
left as a follow-up alongside the real-RBAC (user1 Reader → 403) check.
