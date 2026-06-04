---
title: Terminal Cockpit critique round-2 fixes
description: Second-round critique remediation for the Terminal Cockpit command classifier and session-chapter ladder — quote-aware parsing, real-activity chapter signals, accessible risk announcements, and forced az-login refresh.
tags:
  - terminal
  - ui
---

# Terminal Cockpit critique round-2 fixes

## Motivation

After the first 20 cockpit improvements landed, a re-critique surfaced 24 further
findings. One (a claimed outfmt7-header-as-hit bug) was investigated and refuted —
`parseBlastOutfmt6` already skips `#` comment lines. The remaining 23 are fixed
here. They cluster around four real defects:

1. The command classifier parsed the raw string with single regexes, so quoted
   redirects (`echo "a > b"`), compound commands, and leading env assignments
   (`FOO=bar echo hi`) were misclassified.
2. The session-chapter ladder advanced from a client-side list of *inserted*
   commands, which neither reflected commands the user typed directly nor
   survived a reload, and counted typed-but-unrun previews as activity.
3. The Azure-CLI "re-check" button could be served a 60 s-cached answer, so the
   manual refresh did not actually re-probe `az login`.
4. The screen-reader risk announcement re-narrated on every keystroke and the
   triage row counter miscounted comment lines.

## User-facing change

- **Command risk classification is now quote- and shell-aware.** Compound
  commands (`a && b`, `a | b`, `a; b`) are judged by their worst segment with
  reduced confidence; redirects inside quotes are no longer flagged as local
  writes; leading `VAR=value` env assignments are ignored; `az account set` /
  `az configure` are treated as local context switches (azure-read) rather than
  cloud mutations; `kubectl exec` is medium rather than destructive.
- **Safer-command preview for `kubectl delete`** strips dangerous flags
  (`--force`, `--grace-period`, `--now`, `--cascade`, `--wait`) so the suggested
  `kubectl get …` is genuinely read-only.
- **Session chapters now advance from commands the terminal actually executed.**
  RemoteTerminal reconstructs executed command lines from the PTY input stream
  (Enter-flushed, backspace/Ctrl-C aware, escape sequences stripped) and shares
  them with the cockpit. Typed-but-unrun inserts no longer advance the ladder;
  the executed history persists per session across reloads.
- **The "re-check az login" button forces a cache bypass** (`?force=true`) so it
  truly re-probes sign-in state; both health and az-login polls now retry once on
  transient failure.
- **The aria-live risk announcement is debounced (~400 ms)** so assistive tech is
  not flooded while editing a command. The triage coverage counter now excludes
  `#` comment lines and is relabelled "rows".
- Minor: command-load focus uses `preventScroll`; persisted cockpit session state
  is schema-validated on read and merged with defaults.

## API / IaC diff summary

No backend, API, or IaC changes. Frontend only:

- `web/src/pages/terminal/terminalCockpitModel.ts` — new quote-aware helpers
  (`stripQuoted`, `stripEnvAssignmentPrefix`, `splitShellSegments`,
  `buildKubectlDeleteSafer`); `classifyCommand` splits into segments and folds to
  the worst; `classifySegment` adds the `az account set` branch and demotes
  `kubectl exec`; `deriveChapterSignalsFromActivity` input field renamed
  `insertedCommands` → `executedCommands` with a tightened REVIEW regex.
- `web/src/pages/terminal/TerminalCockpit.tsx` — new required prop
  `executedCommands: string[]`; forced az refresh via `forceAzureRef`; debounced
  `riskAnnouncement`; validated/merged session restore; `handleInsert` no longer
  keeps a separate inserted-command record.
- `web/src/pages/RemoteTerminal.tsx` — `executedCommands` state with
  sessionStorage persistence and an `onData` line-buffer that records genuinely
  executed commands; passes the prop to `TerminalCockpit`.
- `web/src/pages/terminal/terminalCockpitModel.test.ts` — field rename plus new
  cases (az account set, quoted redirect, env-prefix, kubectl exec medium,
  kubectl delete flag stripping).

## Validation evidence

- `cd web && npm test -- --run` → **68 files, 595 tests passing** (30 in
  `terminalCockpitModel.test.ts`).
- `cd web && npm run build` → built in ~6.5 s, no type errors.
- `cd web && npx eslint src/pages/terminal/TerminalCockpit.tsx
  src/pages/terminal/terminalCockpitModel.ts
  src/pages/terminal/terminalCockpitModel.test.ts
  src/pages/RemoteTerminal.tsx` → clean (0 errors, 0 warnings).
