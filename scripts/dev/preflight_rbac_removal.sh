#!/usr/bin/env bash
# preflight_rbac_removal.sh — invoked by the `azure.yaml` preprovision hook
# to halt `azd provision` if the upcoming Bicep change would DELETE any
# Microsoft.Authorization/roleAssignments resource (charter §12a Rule 7).
#
# Default behaviour: read-only what-if + warn-only report. The check is
# gated to halt mode only when STRICT_RBAC_REMOVAL_HALT=true, and an
# acknowledged removal can pass through with ACCEPT_RBAC_REMOVAL=phase-2-of-pr-NN.
#
# Why a shell wrapper:
#   - `azd` resolves ${AZURE_*}/${API_CLIENT_ID=…} placeholders in
#     infra/main.parameters.json when it invokes Bicep. Calling
#     `az deployment sub what-if` directly leaves those unresolved, so we
#     substitute them here with envsubst before passing to az.
#   - The actual parsing/exit-code logic lives in
#     `scripts/dev/check_rbac_removal.py`, which is the pytest target.
#
# Usage (called from azure.yaml preprovision):
#   bash ./scripts/dev/preflight_rbac_removal.sh

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

ts() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# Skip silently when the env doesn't look like an azd run (e.g. unit tests).
if [[ -z "${AZURE_SUBSCRIPTION_ID:-}" || -z "${AZURE_LOCATION:-}" ]]; then
  ts "rbac-guard: AZURE_SUBSCRIPTION_ID or AZURE_LOCATION unset — skipping preflight."
  exit 0
fi

if ! command -v az >/dev/null 2>&1; then
  ts "rbac-guard: az CLI not on PATH — skipping preflight."
  exit 0
fi

# Find a python interpreter — prefer the project's uv venv if present.
PY=""
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY="$REPO_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  ts "rbac-guard: no python interpreter found — skipping preflight."
  exit 0
fi

PARAM_TEMPLATE="$REPO_ROOT/infra/main.parameters.json"
if [[ ! -f "$PARAM_TEMPLATE" ]]; then
  ts "rbac-guard: $PARAM_TEMPLATE missing — skipping preflight."
  exit 0
fi

# envsubst is part of gettext-base; fall back to plain copy if unavailable
# (the file may still resolve from azd-side substitution in some setups).
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
RESOLVED_PARAMS="$WORKDIR/main.parameters.resolved.json"
if command -v envsubst >/dev/null 2>&1; then
  envsubst <"$PARAM_TEMPLATE" >"$RESOLVED_PARAMS"
else
  ts "rbac-guard: envsubst not installed — passing parameters file verbatim."
  cp "$PARAM_TEMPLATE" "$RESOLVED_PARAMS"
fi

WHATIF_JSON="$WORKDIR/whatif.json"
ts "rbac-guard: running 'az deployment sub what-if' against infra/main.bicep ($AZURE_LOCATION)"
if ! az deployment sub what-if \
      --subscription "$AZURE_SUBSCRIPTION_ID" \
      --location "$AZURE_LOCATION" \
      --template-file "$REPO_ROOT/infra/main.bicep" \
      --parameters "$RESOLVED_PARAMS" \
      --no-pretty-print \
      --output json \
      >"$WHATIF_JSON" 2>"$WORKDIR/what-if.err"; then
  ts "rbac-guard: what-if call failed — skipping preflight (see preprovision logs)."
  if [[ -s "$WORKDIR/what-if.err" ]]; then
    sed 's/^/  /' "$WORKDIR/what-if.err" >&2 || true
  fi
  exit 0
fi

# Delegate parse + gate to the python script. Its exit codes:
#   0  no removals OR warn-only mode OR override accepted
#   2  bad CLI usage (shouldn't happen here)
#   3  HALT — strict mode + unaccepted removals
#   4  az/JSON parsing failure
set +e
"$PY" "$SCRIPT_DIR/check_rbac_removal.py" --from-json "$WHATIF_JSON"
RC=$?
set -e

case "$RC" in
  0)
    exit 0
    ;;
  3)
    ts "rbac-guard: refusing to continue — see the per-finding lines above."
    ts "rbac-guard: to acknowledge an intentional phase-2 removal, set"
    ts "rbac-guard:   ACCEPT_RBAC_REMOVAL='phase-2-of-pr-<N>' and re-run azd provision."
    exit "$RC"
    ;;
  *)
    ts "rbac-guard: parser returned exit=$RC; treating as non-fatal (no halt)."
    exit 0
    ;;
esac
