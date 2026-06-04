---
title: Terminal select-to-copy
description: Selecting text in the browser terminal now copies it to the clipboard automatically.
tags:
  - terminal
  - ui
---

# Terminal select-to-copy

## Motivation

Selecting text with the mouse in the browser terminal did not place the
selection on the clipboard. Users had to right-click (PuTTY-style copy) or use a
keyboard shortcut, which was non-obvious and broke the muscle memory of a normal
Linux terminal.

## User-facing change

- Dragging a mouse selection in the ElasticBLAST Terminal now copies the
  highlighted text to the system clipboard as soon as the selection settles
  (Linux/PuTTY "select-to-copy" behaviour).
- The existing right-click copy/paste behaviour is unchanged.

## Implementation

- [web/src/pages/RemoteTerminal.tsx](../../../web/src/pages/RemoteTerminal.tsx):
  added a `mouseup` listener on the xterm container (`handleTerminalMouseUp`)
  that reads `term.getSelection()` and writes it to `navigator.clipboard`. The
  listener is registered alongside the existing capture-phase `contextmenu`
  handler and torn down in the same effect cleanup.
- Clipboard writes are best-effort: failures in an insecure context are ignored,
  matching the existing right-click copy path.

## Validation

- `cd web && npm run build` — passes.
- `npx vitest run src/pages/remoteTerminalProtocol.test.ts` — 4 passed.
