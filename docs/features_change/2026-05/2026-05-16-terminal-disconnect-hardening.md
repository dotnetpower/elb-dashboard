# 2026-05-16 — Terminal disconnect hardening

## Motivation

Users reported the browser terminal repeatedly showing "disconnected"
during normal dev work. Root-cause analysis of `compose-full-containers.log`
identified three pain points:

1. **Terminal sidecar restarts** (manual `docker compose restart terminal`,
   or a real Container App revision swap) drop the upstream WS with code
   1006 and bubble up to the browser as "Disconnected".
2. **Docker / Container Apps DNS race** right after the sidecar comes
   back: the very first `websockets.connect("ws://terminal:7681/ws")`
   sees `[Errno -2] Name or service not known`, the proxy closes the
   browser WS *before* `accept()`, and the browser receives HTTP 403 +
   has to wait through a full backoff cycle.
3. **Slow first reconnect** — 800 ms backoff before *any* retry made a
   recoverable blip feel broken.

There was also benign log noise: `terminal proxy final close failed:
Unexpected ASGI message 'websocket.close'` from a redundant close in the
`finally` block.

## User-facing change

A typical sidecar restart now feels like a screen flicker (~150 ms) on
the terminal page, not a "Disconnected (code 1006) reconnecting in 0.8s"
banner. Reconnect attempts past the first one keep the same exponential
backoff (800 ms → 8 s) and the same "reconnecting…" banner.

## API / backend diff

`api/routes/terminal_ws.py` — `ws_terminal()`:

- Upstream `websockets.connect()` now retries up to 4 times with
  0.2/0.4/0.8 s backoff (≈1.4 s total) on `OSError`, `TimeoutError`,
  `websockets.InvalidHandshake`. Any other exception is non-transient
  and fails fast as before.
- Added `open_timeout=4` to the upstream connect so a stuck DNS lookup
  doesn't hang the request forever.
- Tracks `browser_closed` / `upstream_closed` flags inside the b2u/u2b
  forwarders and skips the redundant `websocket.close()` /
  `upstream.close()` calls in the cleanup paths — kills the noisy
  "Unexpected ASGI message" debug log on every clean close.

## Frontend diff

`web/src/pages/RemoteTerminal.tsx` — reconnect handler:

- First reconnect after an `onclose` is fast (150 ms); attempts 2+ keep
  the exponential 800 ms → 8 s backoff.
- Only writes the yellow "reconnecting in Ns…" banner into the terminal
  starting from the second attempt; the fast first retry just flips the
  status pill to "connecting" with a quiet "Reconnecting…" message.

## Validation

- `uv run pytest -q api/tests` → 207 passed.
- `uv run ruff check api/routes/terminal_ws.py` → clean.
- `cd web && npm run build` → clean build.
- End-to-end repro against the local compose stack: open WS session →
  `docker compose restart terminal` → immediately request a new ticket
  and reconnect. **Before**: first reconnect returned HTTP 403 (DNS race)
  and required a backoff cycle. **After**:

  ```text
  session 1 OK
  session 2 (post-restart) OK in 0.51s
  ```

  api log shows two clean `terminal session connected` lines and no
  `terminal proxy upstream connect failed` / `terminal proxy final close
  failed` entries.

## Notes for reviewers

- No change to ttyd binding (still loopback in prod).
- No change to ticket TTL / single-use semantics.
- Production Container App restart will benefit from the same retry
  loop; behavior is identical to the local repro because
  `TERMINAL_UPSTREAM` is just `http://terminal:7681` swapped from
  service DNS to loopback.
