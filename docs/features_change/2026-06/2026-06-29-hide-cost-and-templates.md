# Hide cost estimate card and submit templates control

## Motivation
Operator requested a leaner UI: the dashboard cost-estimate card and the New
Search submit-templates control are not needed for the current deployment.

## User-facing change
- Dashboard no longer renders the `CostCard` (cost estimate + budget guardrail).
- New Search (BLAST submit) no longer renders the `BlastTemplatesControl` (saved
  submit presets). Components are kept in the tree, just not mounted.

## API / IaC diff
None. Frontend-only (`web/src/pages/Dashboard/DashboardGrid.tsx`,
`web/src/pages/BlastSubmit.tsx`).

## Validation
- `npm run build` — clean.
