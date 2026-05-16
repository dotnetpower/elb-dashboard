# Terminal Wheel Scroll

## Motivation

Mouse wheel input in the browser terminal could reach the ttyd input stream and behave like shell history navigation instead of scrolling the visible terminal buffer.

## User-facing change

The Terminal page now consumes mouse wheel events in the browser terminal frame and maps them to xterm scrollback movement. Wheel gestures scroll the terminal screen instead of recalling previous shell commands.

## API/IaC diff summary

- Added a small terminal wheel-scroll helper for normalising pixel, line, and page wheel deltas.
- Wired the helper through xterm's `attachCustomWheelEventHandler` so the wheel policy runs before xterm can translate wheel input into terminal control sequences.
- Added Vitest coverage for wheel delta conversion and event consumption.
- No backend, terminal image, or IaC changes.

## Validation evidence

- `npm run test -- src/pages/terminal/wheelScroll.test.ts src/pages/remoteTerminalProtocol.test.ts` -> 2 files / 8 tests passed.
- `npm run build` -> TypeScript and Vite production build passed.
- Browser smoke at `/terminal`: Playwright wheel over `.terminal-frame .xterm` produced no WebSocket input frames, so wheel movement no longer reaches ttyd as shell input.
