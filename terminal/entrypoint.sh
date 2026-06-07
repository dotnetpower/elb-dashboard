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

# Ensure the operator home is stable even if the base image contributes a
# different default HOME for another user.
export HOME="${TERMINAL_HOME:-/home/azureuser}"
export USER="${USER:-azureuser}"
export SHELL="${SHELL:-/bin/bash}"
mkdir -p "$HOME/.azure" "$HOME/.kube" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Scaffold ElasticBLAST cfg starting points (idempotent — never clobbers a
# file the user has already edited). The home directory is ephemeral, so this
# runs on every revision start; each write is guarded by `[[ -e ]]` so a user
# who edited the template keeps their changes for the life of the session.
# Values are seeded from the non-secret platform coordinates injected by
# infra/modules/containerAppControl.bicep (AZURE_REGION / AZURE_RESOURCE_GROUP
# / STORAGE_ACCOUNT_NAME / PLATFORM_ACR_NAME).
# ---------------------------------------------------------------------------
scaffold_blast_cfg() {
  local examples="$HOME/examples"
  mkdir -p "$examples" 2>/dev/null || true

  local template="$HOME/elastic-blast.ini.template"
  if [[ ! -e "$template" ]]; then
    # Generate a best-effort template from env defaults. elb-cfg prints a
    # WARNING for still-empty required keys on stderr; we keep stdout only.
    if /usr/local/bin/elb-cfg --program blastn > "$template" 2>/dev/null; then
      :
    else
      rm -f "$template" 2>/dev/null || true
    fi
  fi

  local sample_query="$examples/sample-query.fa"
  if [[ ! -e "$sample_query" ]]; then
    cat > "$sample_query" <<'FASTA' 2>/dev/null || true
>sample_query_1 example nucleotide sequence for a smoke-test BLAST run
ACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT
GGCCTTAAGGCCTTAAGGCCTTAAGGCCTTAAGGCCTTAAGGCCTTAAGGCCTTAAGGCCTTAA
FASTA
  fi

  local readme="$examples/README.txt"
  if [[ ! -e "$readme" ]]; then
    cat > "$readme" <<'TXT' 2>/dev/null || true
ElasticBLAST quick start (browser terminal)
===========================================

1. Generate a config from the platform defaults:

     elb-cfg --program blastn \
             --db blast-db/16S_ribosomal_RNA/16S_ribosomal_RNA \
             --queries sample-query.fa \
             --results results/run-001 \
             -o ~/elastic-blast.ini

2. Validate it:

     elb-cfg --check ~/elastic-blast.ini

3. Submit:

     elastic-blast submit --cfg ~/elastic-blast.ini

Notes
-----
* Region / resource group / storage account / ACR default from the
  environment; override with --region / --rg / --storage-account / --acr-name.
* A bare --queries / --results / --db name is expanded into a full blob URL
  under the matching container (queries / results / blast-db).
* The dashboard "Submit" path is the authority for shard sizing; this helper
  covers the common single-config manual run.
* Your home directory is EPHEMERAL — stage inputs/outputs to Storage with
  azcopy; files left here are lost when the revision restarts.
TXT
  fi
}
scaffold_blast_cfg || true

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
REPORTER_PID=0

shutdown() {
  local sig="$1"
  echo "elb-supervisor: received $sig, forwarding to children" >&2
  if [[ "$REPORTER_PID" -gt 0 ]]; then kill -"$sig" "$REPORTER_PID" 2>/dev/null || true; fi
  if [[ "$EXEC_PID" -gt 0 ]]; then kill -"$sig" "$EXEC_PID" 2>/dev/null || true; fi
  if [[ "$TTYD_PID" -gt 0 ]]; then kill -"$sig" "$TTYD_PID" 2>/dev/null || true; fi
}
trap 'shutdown TERM' SIGTERM
trap 'shutdown INT'  SIGINT
trap 'shutdown HUP'  SIGHUP

# ---------------------------------------------------------------------------
# Start cgroup metrics reporter (background). Publishes this sidecar's
# CPU/MEM into Redis db 2 every REPORT_INTERVAL seconds — read by the api
# sidecar at /api/monitor/sidecars. Crashes are non-fatal: we don't include
# REPORTER_PID in `wait -n` because losing telemetry must NOT cycle the
# revision (ttyd / exec_server failures are the actual liveness signals).
# ---------------------------------------------------------------------------
SIDECAR_NAME="${SIDECAR_NAME:-terminal}"
export SIDECAR_NAME
/opt/elb/venv/bin/python3 /usr/local/bin/elb-cgroup-reporter "$SIDECAR_NAME" &
REPORTER_PID=$!

# ---------------------------------------------------------------------------
# Start exec server (background). Uses python3.12 explicitly so PATH order
# changes can never silently swap interpreters. Give programmatic exec calls a
# separate Azure CLI cache from the interactive browser terminal; API/Celery
# submissions may log in with managed identity, while the user's ttyd shell
# still owns /home/azureuser/.azure.
# ---------------------------------------------------------------------------
mkdir -p "${EXEC_AZURE_CONFIG_DIR:-/tmp/elb-exec-azure}" 2>/dev/null || true
AZURE_CONFIG_DIR="${EXEC_AZURE_CONFIG_DIR:-/tmp/elb-exec-azure}" \
  /usr/bin/python3.12 /usr/local/bin/elb-exec-server &
EXEC_PID=$!

# ---------------------------------------------------------------------------
# Bootstrap a managed-identity `az login` into the EXEC Azure CLI cache (the
# separate config dir the exec server uses — NOT the interactive shell's
# /home/azureuser/.azure). Programmatic exec calls like the self-upgrade
# `az acr build` need an `az` account context; without this they fail with
# "Please run 'az login' to setup account." azcopy / kubectl authenticate via
# MSI directly and are unaffected, which is why this gap only surfaced for the
# `az acr build` path. Best-effort + backgrounded: a login hiccup must never
# block ttyd / exec_server startup, and `az acr build` re-checks the account
# at call time. AZURE_CLIENT_ID is the shared user-assigned MI (set in the
# Container App template); fall back to a system-assigned login when unset.
# ---------------------------------------------------------------------------
(
  _exec_cfg="${EXEC_AZURE_CONFIG_DIR:-/tmp/elb-exec-azure}"
  if [ -n "${AZURE_CLIENT_ID:-}" ]; then
    AZURE_CONFIG_DIR="$_exec_cfg" az login --identity --client-id "$AZURE_CLIENT_ID" \
      --allow-no-subscriptions >/dev/null 2>&1 \
      || echo "elb-supervisor: exec az login --identity (UAMI) failed; az acr build may need it" >&2
  else
    AZURE_CONFIG_DIR="$_exec_cfg" az login --identity \
      --allow-no-subscriptions >/dev/null 2>&1 \
      || echo "elb-supervisor: exec az login --identity (system) failed" >&2
  fi
) &

# ---------------------------------------------------------------------------
# Start ttyd (background). -W = writable shell. -p 7681 -i 127.0.0.1 = loopback.
# -a = url-arg: ttyd forwards the WebSocket `?arg=<token>` query parameter as
# an argument to the launched command. The api proxy derives <token> from the
# authenticated caller's object id (api/routes/terminal/ws.py `_session_arg`),
# so `elb-tmux-attach` creates / re-attaches a PER-OPERATOR tmux session — one
# person never drops into another person's shell, scrollback, or `az login`
# context, while the same person re-attaches their own session after a browser
# refresh. Do not pass tmux `-D`: a reconnect would detach the previous ttyd
# client, whose close handler would schedule another reconnect and create a
# self-sustaining reconnect loop.
# ---------------------------------------------------------------------------
TTYD_HOST="${TTYD_HOST:-127.0.0.1}"
# Charter §9 / AGENTS.md tripwire: ttyd carries the interactive writable shell,
# so in a deployed Container Apps revision (CONTAINER_APP_NAME is always set by
# the platform) it MUST stay loopback-only — the api sidecar proxies it over
# 127.0.0.1. Binding to 0.0.0.0 / a routable interface would expose the shell
# to the whole Container Apps Environment VNet. Mirror the exec_server guard
# (terminal/exec_server.py): hard-fail at startup rather than silently rebind,
# so a misconfigured deploy is visible immediately, not after the first abuse.
if [[ -n "${CONTAINER_APP_NAME:-}" ]]; then
  case "$TTYD_HOST" in
    127.0.0.1 | localhost | ::1) : ;;
    *)
      echo "elb-supervisor: refusing to start ttyd with TTYD_HOST=$TTYD_HOST inside a Container Apps revision; pin to 127.0.0.1" >&2
      exit 1
      ;;
  esac
fi
/usr/local/bin/ttyd \
  -p 7681 \
  -i "$TTYD_HOST" \
  -W \
  -a \
  -t enableZmodem=false \
  -t fontSize=14 \
  /usr/local/bin/elb-tmux-attach &
TTYD_PID=$!

echo "elb-supervisor: ttyd host=$TTYD_HOST pid=$TTYD_PID exec_server pid=$EXEC_PID reporter pid=$REPORTER_PID" >&2

# Block until either critical child (ttyd / exec_server) exits. The reporter
# is intentionally NOT waited on — telemetry loss must not cycle the revision.
wait -n "$TTYD_PID" "$EXEC_PID"
FIRST_RC=$?

shutdown TERM
# Give children up to 5 s to clean up before we exit.
SECONDS=0
while [[ $SECONDS -lt 5 ]] && { kill -0 "$EXEC_PID" 2>/dev/null || kill -0 "$TTYD_PID" 2>/dev/null || kill -0 "$REPORTER_PID" 2>/dev/null; }; do
  sleep 0.5
done
kill -KILL "$REPORTER_PID" 2>/dev/null || true
kill -KILL "$EXEC_PID" 2>/dev/null || true
kill -KILL "$TTYD_PID" 2>/dev/null || true

echo "elb-supervisor: first child exited rc=$FIRST_RC; shutting down sidecar" >&2
exit "$FIRST_RC"
