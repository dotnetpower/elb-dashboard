# Gate action buttons on prerequisites, retire Remote Terminal VM remnants

## Motivation

The dashboard exposed several primary-action buttons that did nothing
useful when their prerequisites were missing:

- "New search" entry points (JobCard, BlastJobs page, BlastResults
  re-submit) stayed active even with zero AKS clusters or all clusters
  stopped — clicking went to BlastSubmit which then displayed
  prerequisite errors.
- "Open Terminal" on the dashboard's Terminal card stayed primary-styled
  even when the card's own banner said the sidecar was unavailable
  (the exact case the user reported).
- BlastResults "Check Terminal" had the same problem.

In addition, the SPA still carried "Remote Terminal VM" wiring that the
Container Apps topology (`.github/copilot-instructions.md` §6, AGENTS.md
tripwire #4) no longer supports: BlastSubmit queried a non-existent VM,
the dashboard's GettingStartedGuide called the same `/api/monitor/terminal`
endpoint that now returns a stub "n/a" payload, the SettingsPanel showed
a "Terminal VM" field, and the top ConfigBar carried a
`vm-elb-terminal` chip.

## User-facing change

- New shared hook `web/src/hooks/usePrerequisites.ts` exposes
  `useClusterReadiness()` and `useTerminalSidecarHealth()` that the
  whole UI shares (a single network call per dashboard load).
- JobCard "New search" → disabled with tooltip when no running AKS
  cluster (`web/src/components/cards/JobCard.tsx`).
- BlastJobs page "New search" → same gate (`web/src/pages/BlastJobs.tsx`).
- BlastResults empty-results panel "Re-submit" / "Check Terminal"
  → respective cluster / sidecar-health gates
  (`web/src/pages/BlastResults.tsx`).
- TerminalCard "Open Terminal" → renders a disabled grey button with a
  tooltip when the sidecar is not reachable; the active link is shown
  only when sidecar health reports `ok`
  (`web/src/components/cards/TerminalCard.tsx`).
- Layout nav: "New Search" and "Terminal" gain a small amber warning
  dot when their prerequisites are missing — links stay clickable so
  the destination page can show its own diagnostic, but the user is
  signaled before clicking (`web/src/components/Layout.tsx`).
- Retired the Remote Terminal VM checks from BlastSubmit (no more
  `vmQuery`, `vmRunning`, `terminalVm`, no `terminal_vm_name` /
  `terminal_resource_group` in pre-flight or submit payloads;
  `web/src/pages/BlastSubmit.tsx`).
- Dashboard's GettingStartedGuide gating now uses
  `useTerminalSidecarHealth()` instead of a VM lookup; copy updated to
  describe the sidecar model
  (`web/src/pages/Dashboard.tsx`, `web/src/components/GettingStartedGuide.tsx`).
- Removed the "Terminal VM" / "Terminal RG" entries from
  `SettingsPanel` and the `terminalVmName` chip from `ConfigBar`.

## API / IaC diff summary

No backend, Bicep, or `pyproject.toml` changes. The optional
`terminal_resource_group` / `terminal_vm_name` fields on the BLAST
submit / pre-flight schemas remain (legacy callers may still send
them; the SPA no longer does). `monitoringApi.terminal` is now unused
by the SPA but kept in `web/src/api/endpoints.ts` since it still has a
real backend stub.

## Validation evidence

- `cd web && npx tsc --noEmit -p .` → only the pre-existing
  `ClusterCard.tsx` `vCPUs`/`memoryGiB` errors remain; all touched
  files are clean.
- Browser smoke (local dev, no terminal sidecar):
  - Dashboard renders one disabled "Open Terminal" button (no
    duplicate), with the "Sidecar unavailable" banner.
  - BLAST Jobs card "New search" is disabled and grey.
  - Top nav shows amber dots next to "New Search" and "Terminal".
  - `vm-elb-terminal` chip no longer rendered in the top ConfigBar.

## Files touched

```
web/src/hooks/usePrerequisites.ts            (new)
web/src/components/cards/JobCard.tsx
web/src/components/cards/TerminalCard.tsx
web/src/components/ConfigBar.tsx
web/src/components/GettingStartedGuide.tsx
web/src/components/Layout.tsx
web/src/components/SettingsPanel.tsx
web/src/pages/BlastJobs.tsx
web/src/pages/BlastResults.tsx
web/src/pages/BlastSubmit.tsx
web/src/pages/Dashboard.tsx
```
