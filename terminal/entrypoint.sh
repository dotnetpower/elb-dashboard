#!/bin/bash
# Entrypoint for the terminal sidecar.
# Starts ttyd bound to loopback, attaching every browser session to a single
# persistent tmux session ("elb"). The api sidecar's WebSocket proxy is what
# the browser actually connects to.

set -euo pipefail

# Ensure $HOME exists and is writable (the Azure Files mount may shadow it).
export HOME="${HOME:-/home/azureuser}"
mkdir -p "$HOME/.azure" "$HOME/.kube" 2>/dev/null || true

# Print the MOTD as part of the first shell login.
cat /etc/motd 2>/dev/null || true

# Run ttyd. -W = writable shell. -p 7681 -i 127.0.0.1 = loopback only.
# Each browser session attaches (or creates) a tmux session named "elb" so
# refreshing the browser does not lose work.
exec /usr/local/bin/ttyd \
  -p 7681 \
  -i 127.0.0.1 \
  -W \
  -t enableZmodem=false \
  -t fontSize=14 \
  bash -lc 'tmux new -A -s elb'
