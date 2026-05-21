#!/usr/bin/env bash
# Idempotently register Azure resource providers required by deployment and first-run workflows.

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: scripts/dev/register-providers.sh [--subscription <id-or-name>]

Environment overrides:
  PROVIDER_REGISTRATION_TIMEOUT_SECONDS  Default: 300 for deployment providers.
  PROVIDER_REGISTRATION_POLL_SECONDS     Default: 5.

The script waits for providers required by azd provision and starts registration
for first-run workload providers such as Compute, ContainerService, and Quota.
USAGE
}

subscription_arg=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --subscription)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --subscription requires a subscription id or name." >&2
        exit 1
      fi
      subscription_arg=(--subscription "$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if ! command -v az >/dev/null 2>&1; then
  echo "ERROR: Azure CLI (az) is required." >&2
  exit 1
fi

timeout_seconds="${PROVIDER_REGISTRATION_TIMEOUT_SECONDS:-300}"
poll_seconds="${PROVIDER_REGISTRATION_POLL_SECONDS:-5}"
if ! [[ "$timeout_seconds" =~ ^[0-9]+$ ]] || ! [[ "$poll_seconds" =~ ^[0-9]+$ ]] || [[ "$timeout_seconds" -eq 0 ]] || [[ "$poll_seconds" -eq 0 ]]; then
  echo "ERROR: PROVIDER_REGISTRATION_TIMEOUT_SECONDS and PROVIDER_REGISTRATION_POLL_SECONDS must be positive integers." >&2
  exit 1
fi

account_name="$(az account show "${subscription_arg[@]}" --query name -o tsv 2>/dev/null || true)"
account_id="$(az account show "${subscription_arg[@]}" --query id -o tsv 2>/dev/null || true)"
if [[ -z "$account_id" ]]; then
  echo "ERROR: no active Azure subscription. Run az login and az account set first." >&2
  exit 1
fi

echo "==> Registering required Azure resource providers for $account_name ($account_id)"

deployment_providers=(
  Microsoft.App
  Microsoft.Authorization
  Microsoft.ContainerRegistry
  Microsoft.Insights
  Microsoft.KeyVault
  Microsoft.ManagedIdentity
  Microsoft.Network
  Microsoft.OperationalInsights
  Microsoft.Resources
  Microsoft.Storage
)

workflow_providers=(
  Microsoft.Compute
  Microsoft.ContainerService
  Microsoft.Quota
)

provider_state() {
  az provider show "${subscription_arg[@]}" -n "$1" --query registrationState -o tsv 2>/dev/null || true
}

register_and_wait() {
  local provider_namespace="$1"
  local state deadline

  state="$(provider_state "$provider_namespace")"
  if [[ "$state" == "Registered" ]]; then
    echo "  ok: $provider_namespace"
    return 0
  fi
  echo "  registering: $provider_namespace${state:+ (current: $state)}"
  az provider register "${subscription_arg[@]}" -n "$provider_namespace" --only-show-errors >/dev/null

  deadline=$((SECONDS + timeout_seconds))
  while [[ "$SECONDS" -lt "$deadline" ]]; do
    state="$(provider_state "$provider_namespace")"
    if [[ "$state" == "Registered" ]]; then
      echo "  ok: $provider_namespace"
      return 0
    fi
    sleep "$poll_seconds"
  done

  state="$(provider_state "$provider_namespace")"
  echo "ERROR: $provider_namespace is still ${state:-unknown} after ${timeout_seconds}s." >&2
  return 1
}

register_best_effort() {
  local provider_namespace="$1"
  local state

  state="$(provider_state "$provider_namespace")"
  if [[ "$state" == "Registered" ]]; then
    echo "  ok: $provider_namespace"
    return 0
  fi

  echo "  registering: $provider_namespace${state:+ (current: $state)}"
  if az provider register "${subscription_arg[@]}" -n "$provider_namespace" --only-show-errors >/dev/null; then
    state="$(provider_state "$provider_namespace")"
    echo "  pending: $provider_namespace${state:+ (current: $state)}"
  else
    echo "  warning: failed to start registration for $provider_namespace" >&2
  fi
}

echo "==> Deployment providers (${#deployment_providers[@]} total)"
for i in "${!deployment_providers[@]}"; do
  provider_namespace="${deployment_providers[$i]}"
  echo "  deployment [$((i + 1))/${#deployment_providers[@]}]: $provider_namespace"
  register_and_wait "$provider_namespace"
done

echo "==> Starting first-run workflow provider registrations"
for i in "${!workflow_providers[@]}"; do
  provider_namespace="${workflow_providers[$i]}"
  echo "  workflow [$((i + 1))/${#workflow_providers[@]}]: $provider_namespace"
  register_best_effort "$provider_namespace"
done

echo "==> Deployment resource providers are ready; first-run workflow provider registration has been requested."