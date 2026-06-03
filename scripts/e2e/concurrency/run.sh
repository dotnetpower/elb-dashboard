#!/usr/bin/env bash
# Orchestrate a live elb-openapi /v1/jobs concurrency probe.
#
# Responsibility: Load the admin token from the cluster (never printed), start
# the read-only pod watcher, run the Python submit/poll harness, then stop the
# watcher and point at the result dir. One self-contained command per scenario.
# Edit boundaries: read-only kubectl + HTTP via harness.py. No cluster mutation
# beyond the BLAST jobs the harness submits. No Azure SDK.
# Usage:
#   scripts/e2e/concurrency/run.sh single                 # baseline, 1 job
#   scripts/e2e/concurrency/run.sh burst 10               # 10 concurrent
#   scripts/e2e/concurrency/run.sh burst 20 --include-heavy
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

MODE="${1:?usage: run.sh <single|burst> [n] [extra harness args...]}"
shift || true
N="10"
if [[ "${1:-}" =~ ^[0-9]+$ ]]; then N="$1"; shift || true; fi

: "${ELB_OPENAPI_FQDN:=elb-openapi-0858f97bac.koreacentral.cloudapp.azure.com}"
: "${KUBECONFIG:=/tmp/elb-kubeconfig}"
export KUBECONFIG ELB_OPENAPI_FQDN

# Pull the admin token from the deployment env (never echo it).
if [[ -z "${X_ELB_API_TOKEN:-}" ]]; then
  X_ELB_API_TOKEN="$(kubectl get deploy -n default elb-openapi \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="ELB_OPENAPI_API_TOKEN")].value}' 2>/dev/null || true)"
fi
if [[ -z "${X_ELB_API_TOKEN:-}" ]]; then
  echo "ERROR: could not load X-ELB-API-Token from cluster (check KUBECONFIG/login)" >&2
  exit 1
fi
export X_ELB_API_TOKEN
echo "token: loaded (len ${#X_ELB_API_TOKEN})" >&2

TS="$(date -u +%Y%m%d-%H%M%S)"
OUTDIR=".logs/e2e/concurrency/${MODE}-${N}-${TS}"
mkdir -p "$OUTDIR"

# Start pod watcher in the background.
bash scripts/e2e/concurrency/watch_pods.sh "$OUTDIR/pods.ndjson" 3 &
WATCH_PID=$!
trap 'kill "$WATCH_PID" 2>/dev/null || true' EXIT
echo "pod watcher pid=$WATCH_PID -> $OUTDIR/pods.ndjson" >&2

# Run the harness.
uv run python scripts/e2e/concurrency/harness.py \
  --mode "$MODE" --n "$N" --outdir "$OUTDIR" "$@"

# Stop watcher.
kill "$WATCH_PID" 2>/dev/null || true
trap - EXIT
echo "results in $OUTDIR" >&2
ls -la "$OUTDIR" >&2
