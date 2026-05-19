# Production Feature Flags

## Motivation

Production operators need a deployment-time switch to hide unfinished or restricted UI surfaces without rebuilding application code for each environment.

## User-facing change

The frontend now supports these runtime feature flags:

- `VITE_FEATURE_CUSTOM_DB`
- `VITE_FEATURE_LAB_TOOLS`
- `VITE_FEATURE_TERMINAL`

Unset or empty values default to enabled. Values such as `false`, `0`, `no`, `off`, and `disabled` disable the feature.

When disabled, the matching navigation entry is hidden and direct route access redirects to the Dashboard. Disabling Terminal also hides the Dashboard terminal card, Terminal keyboard shortcut, and secondary Terminal links.

Hardening pass: feature-gated pages are lazy-loaded so disabled Terminal, Lab Tools, and Custom DB surfaces are not fetched in the initial bundle. The Getting Started checklist also omits the Terminal step when the Terminal flag is disabled.

## API / IaC / deployment diff

- No backend API contract changes.
- `web/entrypoint.sh` writes the three flags into `runtime-config.js` from container environment variables.
- `web/Dockerfile`, `scripts/dev/quick-deploy.sh`, and `scripts/dev/postprovision.sh` pass the flags through build/runtime deployment paths.
- `infra/main.bicep` and `infra/modules/containerAppControl.bicep` expose the flags for the bundled Container App frontend sidecar.

## Validation

- `npm run test -- runtime jobMapping`
- `npm run build`
- Hardening build split verified: `RemoteTerminal-*.js`, `ToolsPage-*.js`, and the custom DB chunk are emitted separately from the main bundle.
- `npx eslint src/config/runtime.ts src/config/runtime.test.ts src/App.tsx src/components/Layout.tsx src/components/KeyboardShortcuts.tsx src/hooks/usePrerequisites.ts src/pages/Dashboard/DashboardGrid.tsx src/pages/Dashboard/useGettingStartedReadiness.ts src/pages/blastResults/BlastResultsTable.tsx src/pages/blastResults/useBlastResultsState.ts src/pages/tools/tabs/DbVersionsTab.tsx src/vite-env.d.ts --max-warnings 0`
- `az bicep build --file infra/main.bicep --outfile /tmp/elb-dashboard-main.json`
- Production quick deploy: `scripts/dev/quick-deploy.sh frontend featureflags-20260519043811` with all three flags set to `false`.
- Live check: `/runtime-config.js` returns `VITE_FEATURE_CUSTOM_DB=false`, `VITE_FEATURE_LAB_TOOLS=false`, and `VITE_FEATURE_TERMINAL=false`; `/api/health` returns revision `ca-elb-control--0000068`.
- Hardened production quick deploy: `scripts/dev/quick-deploy.sh frontend featureflags-hardened-20260519044516` with all three flags set to `false`.
- Hardened live check: latest ready revision is `ca-elb-control--0000069`, all six sidecars are ready with zero restarts, `/runtime-config.js` returns the three flags as `false`, and `/api/health` returns revision `ca-elb-control--0000069`.
- Hardened public HTML check: initial HTML references only `assets/index-BTV9Q6Tl.js` and `assets/index-ZiarHJgc.css`; Terminal and Tools page chunks are no longer initial assets.

Full `npm run lint` still reports existing React hook dependency warnings in `src/pages/apiReference/EndpointCard.tsx` and `src/pages/blastSubmit/useDbWithWarmupPlan.ts`; those files are outside this change.