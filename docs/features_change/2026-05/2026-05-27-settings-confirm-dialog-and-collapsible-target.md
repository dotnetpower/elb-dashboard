# Settings panel: replace `window.confirm` with glass `ConfirmDialog` and collapse Provision target fields

## Motivation

The Telemetry section of the Settings panel was the last surface still using native `window.confirm`
dialogs (Provision App Insights, Clear from deployment, Reset preferences). On non-English
operating systems the buttons rendered as 확인/취소 — a direct violation of the English-only UI
language policy (charter §2) — and the OS-styled dialog with the `<host>의 메시지` origin prefix
broke the glass theme (charter §10). It was also blocking the JS thread synchronously inside
`useCallback`.

The Provision form additionally exposed Subscription ID, Resource group, and Region as always-on
input fields. These are prefilled from the Setup Wizard config and are almost never edited; they
added noise above the fields that actually need user attention (Application Insights name,
Log Analytics workspace name, Retention).

## User-facing change

- Three confirmations now use the existing `ConfirmDialog` glass modal:
  - **Provision App Insights** — primary-tone button, the 4 summary lines (RG / App Insights name /
    Log Analytics name / Retention) render as a bullet list inside the dialog. Cost notice moves
    to a footnote.
  - **Clear from deployment** — danger-tone button.
  - **Reset preferences** — danger-tone button.
- Provision target fields (Subscription ID, Resource group, Region) collapse into a single
  read-only summary row at the top of the form (e.g. `Target  <last-12-of-subscription> / rg-elb-dashboard / koreacentral  [Change]`).
  - Click **Change** to expand the editable inputs.
  - Auto-expands and disables the toggle when any of the three fields is empty, has a validation
    error, or when the region differs from the workload region. A `region mismatch` badge is
    surfaced in the summary in that case so users still see the warning when collapsed.
- The confirmation dialog content, validation, and submitted payload are unchanged.

## API/IaC diff summary

No backend, no Bicep changes.

Frontend:

- [web/src/components/ConfirmDialog.tsx](../../../web/src/components/ConfirmDialog.tsx)
  - Added optional `details?: string[]` (rendered as a bullet list), `footnote?: string`, and
    `tone?: "danger" | "primary"` props. Default `tone="danger"` preserves the previous behaviour
    for all existing call sites (WorkspaceDiagnosticsBanner, ClusterCard, BlastResults, BlastJobs,
    ComputeSection).
- [web/src/components/SettingsPanel.tsx](../../../web/src/components/SettingsPanel.tsx)
  - Removed all three `window.confirm` calls.
  - Split `provision()` / `clearFromDeployment()` into open-dialog + submit pairs.
  - Wrapped Subscription/RG/Region into a collapsible block with `targetExpanded` state and an
    auto-expand guard (`targetHasError || targetHasEmpty || regionMismatch`).
  - Added `ChevronDown` / `ChevronRight` lucide imports.

## Validation evidence

- `npx eslint src/components/SettingsPanel.tsx src/components/ConfirmDialog.tsx` — clean.
- `npx tsc -p tsconfig.json --noEmit` — clean.
- `npm run build` — `✓ built in 6.52s`, no new warnings.
- Manual review of all existing `ConfirmDialog` callers confirmed back-compat (new props are
  optional with prior defaults).
