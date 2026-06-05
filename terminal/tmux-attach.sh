#!/bin/bash
# Per-operator tmux session launcher for the browser terminal sidecar.
#
# Why this exists
# ---------------
# ttyd was previously started with a fixed `tmux new-session -A -s elb`, so
# EVERY browser operator attached to the single shared `elb` session — the
# same PTY, the same scrollback, and the same `az login` context. One person
# would open the terminal and see another person's commands, device codes, and
# working directory. (Tracked as issue #2 in
# docs/copilot/security-audit-followup.md.)
#
# Contract
# --------
# ttyd is now started with `--url-arg`, and the api WebSocket proxy
# (api/routes/terminal/ws.py `_session_arg`) appends `?arg=<token>` where the
# token is a non-reversible hash of the authenticated caller's object id. ttyd
# forwards that as $1 to this wrapper, so:
#   * each distinct operator gets — or, on first connect, creates — their OWN
#     tmux session and never lands in another operator's shell;
#   * a browser refresh by the same operator re-derives the same token and
#     re-attaches, preserving in-progress work (`new-session -A`).
#
# Without an argument (manual `kubectl exec` into the sidecar, or local compose
# without the proxy) it falls back to a single shared session named
# "elb-shared" so the terminal still works for debugging.

set -uo pipefail

raw="${1:-}"
# Sanitise to a safe tmux session-name suffix: lowercase alphanumerics only,
# capped at 40 chars. The proxy only ever sends [a-z0-9], so this is
# defence-in-depth — and because the value is used purely as a tmux session
# name passed via argv (never shell-evaluated), there is no command-injection
# surface even for a malformed arg.
clean="$(printf '%s' "$raw" | tr -cd 'a-z0-9' | cut -c1-40)"
suffix="${clean:-shared}"
session="elb-${suffix}"

# Per-operator Azure CLI credential cache. Isolating the tmux PTY alone is not
# enough: every interactive shell shares $HOME (/home/azureuser), so without
# this one operator's `az login` token in $HOME/.azure would be reused by
# another operator's (now PTY-isolated) shell — they could run
# az / azcopy / elastic-blast as the first operator's identity. A per-session
# AZURE_CONFIG_DIR gives each operator their own token cache. azcopy honours it
# too: profile.sh sets AZCOPY_AUTO_LOGIN_TYPE=AZCLI, which shells out to `az`,
# and `az` reads AZURE_CONFIG_DIR. We pass it with tmux `-e` so the value lands
# in the session environment of the shell tmux spawns; on `-A` re-attach by the
# same operator the suffix (hence the dir) is identical, so it stays coherent.
azure_dir="$HOME/.azure-${suffix}"
mkdir -p "$azure_dir" 2>/dev/null || true

exec /usr/bin/tmux new-session -A -s "$session" \
  -e "AZURE_CONFIG_DIR=$azure_dir" \
  /bin/bash --login
