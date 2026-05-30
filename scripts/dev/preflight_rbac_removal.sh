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

# Tighten file mode mask for any temp files we create — the parameters
# document includes principal ids that should not be world-readable.
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

START_EPOCH=$(date +%s)

ts() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# Final summary line — always runs (success, halt, or error) so the
# outcome is visible at the bottom of long preprovision logs.
WORKDIR=""
FINAL_STATUS="unknown"
cleanup_and_summarise() {
  local rc=$?
  local elapsed=$(( $(date +%s) - START_EPOCH ))
  if [[ -n "$WORKDIR" && -d "$WORKDIR" ]]; then
    rm -rf "$WORKDIR" 2>/dev/null || true
  fi
  ts "rbac-guard: preflight complete (exit=$rc, status=$FINAL_STATUS, elapsed=${elapsed}s)"
}
trap cleanup_and_summarise EXIT INT TERM ERR

# Surface the active mode up front so the operator immediately sees
# whether a failure would be advisory or fatal.
STRICT_FLAG="${STRICT_RBAC_REMOVAL_HALT:-}"
ACCEPT_FLAG="${ACCEPT_RBAC_REMOVAL:-}"
if [[ -n "$STRICT_FLAG" ]]; then
  ts "rbac-guard: mode=STRICT (STRICT_RBAC_REMOVAL_HALT=$STRICT_FLAG)"
else
  ts "rbac-guard: mode=WARN-ONLY (STRICT_RBAC_REMOVAL_HALT unset; charter §12a Rule 4 default-OFF)"
fi
if [[ -n "$ACCEPT_FLAG" ]]; then
  ts "rbac-guard: ACCEPT_RBAC_REMOVAL is set (token will be validated by check_rbac_removal.py)"
fi

# Helper: in STRICT mode, an internal failure (env missing, az not on PATH,
# what-if call failed) is too risky to silent-skip because it could hide
# the very deletion we are guarding against. WARN-ONLY mode keeps the
# legacy silent-skip behaviour so it never accidentally blocks normal
# development.
strict_or_skip() {
  local reason="$1"
  if [[ -n "$STRICT_FLAG" ]]; then
    ts "rbac-guard: STRICT mode and $reason — refusing to skip preflight."
    FINAL_STATUS="halt-internal-failure"
    exit 3
  fi
  ts "rbac-guard: $reason — skipping preflight (warn-only mode)."
  FINAL_STATUS="skipped"
  exit 0
}

# Skip silently when the env doesn't look like an azd run (e.g. unit tests).
if [[ -z "${AZURE_SUBSCRIPTION_ID:-}" || -z "${AZURE_LOCATION:-}" ]]; then
  strict_or_skip "AZURE_SUBSCRIPTION_ID or AZURE_LOCATION unset"
fi

if ! command -v az >/dev/null 2>&1; then
  strict_or_skip "az CLI not on PATH"
fi

# Find a python interpreter — prefer the project's uv venv if present.
# `-x` is not enough: a broken symlink (deleted venv after build) reports
# executable but `exec` fails, so probe with a no-op import.
PY=""
if [[ -e "$REPO_ROOT/.venv/bin/python" ]]; then
  if "$REPO_ROOT/.venv/bin/python" -c '' 2>/dev/null; then
    PY="$REPO_ROOT/.venv/bin/python"
  else
    ts "rbac-guard: $REPO_ROOT/.venv/bin/python exists but is not executable — falling back."
  fi
fi
if [[ -z "$PY" ]] && command -v python3 >/dev/null 2>&1; then
  PY="python3"
fi
if [[ -z "$PY" ]] && command -v python >/dev/null 2>&1; then
  PY="python"
fi
if [[ -z "$PY" ]]; then
  strict_or_skip "no python interpreter found"
fi

PARAM_TEMPLATE="$REPO_ROOT/infra/main.parameters.json"
TEMPLATE_FILE="$REPO_ROOT/infra/main.bicep"
if [[ ! -f "$PARAM_TEMPLATE" ]]; then
  strict_or_skip "$PARAM_TEMPLATE missing"
fi
if [[ ! -f "$TEMPLATE_FILE" ]]; then
  strict_or_skip "$TEMPLATE_FILE missing"
fi

WORKDIR="$(mktemp -d)"

RESOLVED_PARAMS="$WORKDIR/main.parameters.resolved.json"
if command -v envsubst >/dev/null 2>&1; then
  envsubst <"$PARAM_TEMPLATE" >"$RESOLVED_PARAMS"
  # envsubst leaves unresolved ${VAR} tokens in place when VAR is unset.
  # Warn loudly so the operator knows the what-if call may fail with
  # an unhelpful "InvalidTemplate" — a hint to set the missing env var.
  if grep -Eo '\$\{[A-Z_][A-Z0-9_]*\}' "$RESOLVED_PARAMS" >/dev/null 2>&1; then
    UNRESOLVED=$(grep -Eo '\$\{[A-Z_][A-Z0-9_]*\}' "$RESOLVED_PARAMS" \
                   | sort -u | head -5 | tr '\n' ' ')
    ts "rbac-guard: WARN unresolved placeholders in parameters.json: $UNRESOLVED"
    ts "rbac-guard: WARN set these env vars (azd env set …) before az can validate."
  fi
else
  ts "rbac-guard: WARN envsubst not installed — install with 'sudo apt install gettext-base' (or 'brew install gettext')."
  ts "rbac-guard: WARN passing parameters file verbatim; placeholders will be sent to az unresolved."
  cp "$PARAM_TEMPLATE" "$RESOLVED_PARAMS"
fi

WHATIF_JSON="$WORKDIR/whatif.json"
WHATIF_ERR="$WORKDIR/what-if.err"
ts "rbac-guard: running 'az deployment sub what-if' against infra/main.bicep ($AZURE_LOCATION)"
if ! az deployment sub what-if \
      --subscription "$AZURE_SUBSCRIPTION_ID" \
      --location "$AZURE_LOCATION" \
      --template-file "$TEMPLATE_FILE" \
      --parameters "$RESOLVED_PARAMS" \
      --no-pretty-print \
      --output json \
      >"$WHATIF_JSON" 2>"$WHATIF_ERR"; then
  ts "rbac-guard: what-if call failed (see captured stderr below)."
  if [[ -s "$WHATIF_ERR" ]]; then
    sed 's/^/  /' "$WHATIF_ERR" >&2 || true
  fi
  strict_or_skip "what-if call failed"
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
    FINAL_STATUS="ok"
    exit 0
    ;;
  3)
    ts "rbac-guard: refusing to continue — see the per-finding lines above."
    ts "rbac-guard: to acknowledge an intentional phase-2 removal, set"
    ts "rbac-guard:   ACCEPT_RBAC_REMOVAL='phase-2-of-pr-<N>' and re-run azd provision."
    FINAL_STATUS="halt"
    exit "$RC"
    ;;
  4)
    ts "rbac-guard: parser returned exit=4 (az/JSON parsing failure)."
    if [[ -n "$STRICT_FLAG" ]]; then
      FINAL_STATUS="halt-parser-failure"
      exit "$RC"
    fi
    FINAL_STATUS="parser-failure-skipped"
    exit 0
    ;;
  *)
    ts "rbac-guard: parser returned unexpected exit=$RC; treating as non-fatal (no halt)."
    FINAL_STATUS="unexpected-rc-$RC"
    exit 0
    ;;
esac
