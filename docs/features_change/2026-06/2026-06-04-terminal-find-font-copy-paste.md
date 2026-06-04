---
title: Terminal find, font zoom, and copy/paste keybindings
description: The browser terminal gains an in-buffer search overlay, font zoom controls, clickable links, and PuTTY-style copy/paste keyboard shortcuts.
tags:
  - terminal
  - ui
---

# Terminal find, font zoom, and copy/paste keybindings

## Motivation

The browser terminal lacked the everyday ergonomics of a native terminal:
there was no way to search scrollback, adjust the font size, click a URL, or use
familiar copy/paste shortcuts. Phase 2 closes that gap so a researcher can work
in the browser terminal the way they would in a local one.

## User-facing change

- **Find in buffer:** a "Find" toolbar button (or `Ctrl/Cmd+Shift+F`) opens a
  search overlay that highlights matches in the scrollback as you type.
- **Font zoom:** toolbar `−` / size / `+` controls, plus `Ctrl/Cmd +`,
  `Ctrl/Cmd -`, and `Ctrl/Cmd 0` (reset), resize the terminal font between 9 and
  28 px without triggering browser page zoom.
- **Clickable links:** URLs in terminal output are now clickable.
- **Copy/paste shortcuts:** `Ctrl/Cmd+Shift+C` copies the current selection and
  `Ctrl/Cmd+Shift+V` pastes, matching common Linux-terminal muscle memory. The
  existing right-click and select-to-copy behaviour is unchanged.

## Implementation

- [web/src/pages/RemoteTerminal.tsx](../../../web/src/pages/RemoteTerminal.tsx):
  loads `@xterm/addon-search` and `@xterm/addon-web-links`; `syncTerminalResize`
  is shared by the `ResizeObserver` and the font-zoom path;
  `applyTerminalFontSize` / `adjustTerminalFontSize` clamp to
  `TERMINAL_FONT_MIN`/`MAX`. `attachCustomKeyEventHandler` handles every shortcut
  with `event.preventDefault()` so the browser default (page zoom, hidden-textarea
  paste) never fires.
- [web/src/theme/glass.css](../../../web/src/theme/glass.css): `.terminal-toolbar`,
  `.terminal-font-controls`, and `.terminal-search` styles; `.terminal-frame`
  becomes `position: relative` to anchor the search overlay.
- [web/package.json](../../../web/package.json): adds `@xterm/addon-search` and
  `@xterm/addon-web-links`.

## Validation

- `cd web && npm run build` — passes; `npx eslint src/pages/RemoteTerminal.tsx`
  — clean.
- `cd web && npm test -- --run` — 581 passed.
