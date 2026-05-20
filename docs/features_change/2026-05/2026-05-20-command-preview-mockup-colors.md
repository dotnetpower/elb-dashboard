# Command Preview Mockup Colors

## Motivation

Align the New Search command preview with the darker command-preview mockup treatment.

## User-facing change

The BLAST submit command preview now uses a dark terminal-style command block with a Microsoft-blue left accent and subtle token colors for commands, flags, values, and numbers. Copy behavior is unchanged.

## API / IaC diff summary

No API or IaC changes. The update is limited to the React command preview rendering and light-theme submit CSS.

## Validation evidence

- `cd web && npm run build`
- `npx eslint src/pages/blastSubmit/ui.tsx --ext ts,tsx --report-unused-disable-directives --max-warnings 0`
- `npx prettier --check src/pages/blastSubmit/ui.tsx src/theme/blast-submit-layout.css`