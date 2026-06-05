#!/usr/bin/env bash
# On-demand runtime proof that ttyd's `-a` (--url-arg) forwards a WebSocket
# `/ws?arg=<token>` query parameter to argv[1] of the launched command.
#
# This is the load-bearing assumption behind the per-operator browser-terminal
# session isolation (terminal/tmux-attach.sh + api/routes/terminal/ws.py
# `_build_upstream_url`). The pytest guards lock the shell/Python logic; this
# script proves the ttyd plumbing itself. Re-run it after a ttyd version bump.
#
# Not wired into CI: it downloads the ttyd release binary and needs outbound
# network. Run manually:  bash scripts/dev/verify_ttyd_url_arg.sh
#
# Verified PASS on 2026-06-05 with ttyd 1.7.7 (argv[1]='uprobe1234').
set -euo pipefail

TTYD_VERSION="${TTYD_VERSION:-1.7.7}"
PORT="${TTYD_PROBE_PORT:-7799}"
PROBE="uprobe1234"
TTYD_BIN="${TTYD_BIN:-/tmp/ttyd-${TTYD_VERSION}}"

if [[ ! -x "$TTYD_BIN" ]]; then
  echo "Downloading ttyd ${TTYD_VERSION} -> ${TTYD_BIN}"
  curl --fail --silent --show-error --location --max-time 120 \
    "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
    -o "$TTYD_BIN"
  chmod +x "$TTYD_BIN"
fi

WRAPPER="$(mktemp)"
GOT="$(mktemp)"
cat > "$WRAPPER" <<WRAP
#!/bin/bash
printf '%s' "\${1:-NOARG}" > "$GOT"
exec sleep 0.3
WRAP
chmod +x "$WRAPPER"
: > "$GOT"

"$TTYD_BIN" -p "$PORT" -i 127.0.0.1 -a -W "$WRAPPER" >/tmp/ttyd-probe.log 2>&1 &
TTYD_PID=$!
trap 'kill "$TTYD_PID" 2>/dev/null || true; rm -f "$WRAPPER"' EXIT
sleep 1.2

python3 - "$PORT" "$PROBE" <<'PY'
import base64, os, socket, sys, time

port, probe = int(sys.argv[1]), sys.argv[2]
key = base64.b64encode(os.urandom(16)).decode()
req = (
    f"GET /ws?arg={probe} HTTP/1.1\r\n"
    f"Host: 127.0.0.1:{port}\r\n"
    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\n"
    "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: tty\r\n\r\n"
)
s = socket.create_connection(("127.0.0.1", port), timeout=5)
s.sendall(req.encode())
resp = s.recv(4096)
assert b"101" in resp.split(b"\r\n", 1)[0], f"no WS upgrade: {resp[:80]!r}"

def frame(p: bytes) -> bytes:
    m = os.urandom(4)
    masked = bytes(b ^ m[i % 4] for i, b in enumerate(p))
    return bytes([0x81, 0x80 | len(p)]) + m + masked

s.sendall(frame(b'{"AuthToken":""}'))
s.sendall(frame(b'1{"columns":80,"rows":24}'))
time.sleep(1.0)
s.close()
PY

sleep 0.4
echo "=== RESULT ==="
got="$(cat "$GOT" 2>/dev/null || true)"
rm -f "$GOT"
if [[ "$got" == "$PROBE" ]]; then
  echo "argv[1]='$got'"
  echo "PASS: ttyd -a forwards ?arg= to argv[1]"
else
  echo "FAIL: expected '$PROBE', got '${got:-<empty>}'"
  echo "--- ttyd-probe.log ---"; cat /tmp/ttyd-probe.log
  exit 1
fi
