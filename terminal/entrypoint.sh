#!/bin/bash
# Entrypoint for the terminal sidecar.
#
# Runs two services side-by-side, both bound to loopback:
#   * ttyd        on 127.0.0.1:7681  — interactive browser shell (proxied
#                                       by the api sidecar /api/terminal/ws)
#   * exec_server on 127.0.0.1:7682  — programmatic shell channel for
#                                       Celery tasks (api / worker call it
#                                       via api/services/terminal_exec.py).
#
# Supervisor model
# ----------------
# This script IS PID 1 inside the container (no `exec`); it forwards SIGTERM
# / SIGINT to both children, waits for whichever exits first, and then exits
# itself with that child's status code so Container Apps restarts the
# revision when either service dies.
#
# Why not `exec ttyd …` with a background watchdog?  Once we exec into ttyd,
# the watchdog gets reparented to the new PID 1 (ttyd). When ttyd dies the
# watchdog's `kill -TERM 1` then targets a non-existent PID and the
# container hangs as a zombie. The supervisor pattern below avoids that
# whole class of bug.

set -uo pipefail

# Ensure $HOME exists and is writable (the Azure Files mount may shadow it).
export HOME="${HOME:-/home/azureuser}"
mkdir -p "$HOME/.azure" "$HOME/.kube" 2>/dev/null || true

# Print the MOTD as part of the first shell login.
cat /etc/motd 2>/dev/null || true

if [[ -z "${EXEC_TOKEN:-}" ]]; then
  echo "WARNING: EXEC_TOKEN is empty — exec server will refuse to start," >&2
  echo "         api/worker sidecars will not be able to call shell tooling." >&2
fi

# ---------------------------------------------------------------------------
# Forward TERM/INT/HUP to children so Container Apps shutdown is graceful.
# ---------------------------------------------------------------------------
TTYD_PID=0
EXEC_PID=0

shutdown() {
  local sig="$1"
  echo "elb-supervisor: received $sig, forwarding to children" >&2
  if [[ "$EXEC_PID" -gt 0 ]]; then kill -"$sig" "$EXEC_PID" 2>/dev/null || true; fi
  if [[ "$TTYD_PID" -gt 0 ]]; then kill -"$sig" "$TTYD_PID" 2>/dev/null || true; fi
}
trap 'shutdown TERM' SIGTERM
trap 'shutdown INT'  SIGINT
trap 'shutdown HUP'  SIGHUP

# ---------------------------------------------------------------------------
# Start exec server (background). Uses python3.12 explicitly so PATH order
# changes can never silently swap interpreters.
# ---------------------------------------------------------------------------
/usr/bin/python3.12 /usr/local/bin/elb-exec-server &
EXEC_PID=$!

# ---------------------------------------------------------------------------
# Start ttyd (background). -W = writable shell. -p 7681 -i 127.0.0.1 = loopback.
# Each browser session attaches (or creates) a tmux session named "elb" so
# refreshing the browser does not lose work.
# ---------------------------------------------------------------------------
/usr/local/bin/ttyd \
  -p 7681 \
  -i 127.0.0.1 \
  -W \
  -t enableZmodem=false \
  -t fontSize=14 \
  bash -lc 'tmux new -A -s elb' &
TTYD_PID=$!

echo "elb-supervisor: ttyd pid=$TTYD_PID exec_server pid=$EXEC_PID" >&2

# Block until either child exits. `wait -n` returns the exit status of the
# first child to finish, then we reap and exit with that status so Container
# Apps observes a non-zero/zero exit and restarts the revision.
wait -n
FIRST_RC=$?

shutdown TERM
# Give children up to 5 s to clean up before we exit.
SECONDS=0
while [[ $SECONDS -lt 5 ]] && { kill -0 "$EXEC_PID" 2>/dev/null || kill -0 "$TTYD_PID" 2>/dev/null; }; do
  sleep 0.5
done
kill -KILL "$EXEC_PID" 2>/dev/null || true
kill -KILL "$TTYD_PID" 2>/dev/null || true

echo "elb-supervisor: first child exited rc=$FIRST_RC; shutting down sidecar" >&2
exit "$FIRST_RC"
