# Terminal Cockpit

## Motivation

The Browser Terminal needs to be more than a raw shell for non-Linux researchers. It should make command intent, risk, workflow state, and recovery options visible without pretending to automate actions that still require user approval.

## User-facing change

- Added a Terminal Cockpit side panel on the Browser Terminal page.
- Added command intent preview with impact/risk classification, pre-flight checks, rollback notes, safer-command suggestions, copy, and insert actions.
- Added a workflow command palette for Azure login/context checks, tool version checks, file inspection, FASTA stats, local BLAST, local database build, and Kubernetes read-only inspection.
- Added terminal health/context indicators backed by the existing `/api/terminal/health` endpoint and current WebSocket state.
- Added session chapters and an innovation coverage matrix that maps the proposed advanced terminal concepts to live, guarded, or foundation states.
- High-risk commands cannot be inserted directly into the live terminal; multiline/control-character paste payloads are normalised before insertion.
- Added molecular-diagnostic workflow presets for Pathogen ID, 16S / ITS ID, AMR gene screening, primer specificity, and custom DB validation.
- Added sample context fields, control-sample awareness, diagnostic guard warnings, BLAST outfmt 6 triage, and evidence-summary runbook copy.
- Added privacy and interpretation hardening: sample IDs that look identifying are blocked by a critical guard, NTC / negative-control hits are flagged, and generated summaries explicitly remain evidence summaries rather than diagnostic conclusions.

## API / IaC diff summary

- No backend or IaC changes.
- Reuses the existing authenticated `/api/terminal/health` endpoint.
- Terminal command execution remains user-controlled; the Cockpit only copies or inserts commands into the shell.

## Validation evidence

- `cd web && npm run test -- src/pages/terminal/terminalDiagnosticModel.test.ts src/pages/terminal/terminalCockpitModel.test.ts src/pages/terminal/wheelScroll.test.ts src/pages/remoteTerminalProtocol.test.ts` -> 4 files / 22 tests passed.
- `cd web && npm run build` -> passed.
- Browser smoke at `http://127.0.0.1:8090/terminal` confirmed the Cockpit panel, Command Preview, Diagnostic Context, Diagnostic Guards, BLAST Result Triage, Workflow Palette, Session Chapters, Innovation Coverage, and terminal frame render together without overlap.
