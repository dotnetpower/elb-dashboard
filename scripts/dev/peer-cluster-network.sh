#!/usr/bin/env bash
# peer-cluster-network.sh — bidirectionally peer the dashboard platform VNet
# with an AKS cluster's auto-created VNet so the api sidecar can reach the
# `elb-openapi` Service's internal LoadBalancer IP.
#
# Why this exists
# ---------------
# `api.tasks.azure.provision.provision_aks` (2026-05-27+) auto-peers the
# two VNets at the end of cluster create. Existing clusters created before
# that step shipped (and any cluster where the auto-peer step recorded
# `vnet_peering.error`) need a one-shot recovery. Without the peering the
# SPA's API Reference page hangs at "Sending..." and the api sidecar logs
# show `openapi/proxy: upstream request failed for http://10.224.0.x:` /
# `openapi/spec: fetch failed ... timed out` — even though the
# `elb-openapi` pods + Service endpoints are healthy.
#
# This script is a shell wrapper around the same idempotent helper the
# Celery task calls (`POST /api/aks/peer-with-platform`). It hits the
# deployed dashboard's HTTPS endpoint directly so a tenant admin can fix
# an env without `azd` / `az aks get-credentials`.
#
# Usage:
#   scripts/dev/peer-cluster-network.sh                       # auto-detect from azd env + AKS list
#   scripts/dev/peer-cluster-network.sh --cluster-name elb-cluster-01 --cluster-rg rg-elb-cluster
#   scripts/dev/peer-cluster-network.sh --container-app ca-elb-dashboard --rg rg-elb-dashboard
#   scripts/dev/peer-cluster-network.sh --dry-run
#   scripts/dev/peer-cluster-network.sh --yes                 # skip the "proceed?" prompt
#
# Auto-detection rules:
#   * Container App name + RG  — from `azd env get-values` keys
#     `CONTAINER_APP_NAME` and `AZURE_RESOURCE_GROUP`.
#   * AKS cluster name + RG    — when the subscription has exactly one AKS
#     cluster, use it. Multiple clusters → refuse and ask for --cluster-name.
#
# Exit codes:
#   0  every direction was already peered or newly peered
#   2  one direction failed (caller lacks Network Contributor on the AKS-auto VNet)
#   3  preconditions not met (az not logged in, no AKS, dashboard URL unreachable)

set -Eeuo pipefail

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
ts()     { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die()    { red "ERROR: $*" >&2; exit "${2:-3}"; }

usage() {
  sed -n '2,40p' "$0"
  exit "${1:-1}"
}

CONTAINER_APP=""
RESOURCE_GROUP=""
SUBSCRIPTION=""
CLUSTER_NAME=""
CLUSTER_RG=""
DRY_RUN=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container-app|--app)   CONTAINER_APP="$2";   shift 2 ;;
    --rg|--resource-group)   RESOURCE_GROUP="$2";  shift 2 ;;
    --subscription)          SUBSCRIPTION="$2";    shift 2 ;;
    --cluster-name)          CLUSTER_NAME="$2";    shift 2 ;;
    --cluster-rg)            CLUSTER_RG="$2";      shift 2 ;;
    --dry-run)               DRY_RUN=1;            shift ;;
    --yes|-y)                ASSUME_YES=1;         shift ;;
    -h|--help)               usage 0 ;;
    *)                       die "unknown flag: $1" ;;
  esac
done

command -v az >/dev/null 2>&1 || die "az CLI not found"
command -v curl >/dev/null 2>&1 || die "curl not found"

# --- Subscription -----------------------------------------------------------
if [[ -z "$SUBSCRIPTION" ]]; then
  SUBSCRIPTION="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv 2>/dev/null || true)}"
  [[ -n "$SUBSCRIPTION" ]] || die "no subscription set; run 'az login' or pass --subscription"
fi
SUB_FLAG=(--subscription "$SUBSCRIPTION")
az account set "${SUB_FLAG[@]}" >/dev/null 2>&1 || die "could not set subscription $SUBSCRIPTION"

# --- Container App auto-detect ---------------------------------------------
if [[ -z "$CONTAINER_APP" ]]; then CONTAINER_APP="${CONTAINER_APP_NAME:-}"; fi
if [[ -z "$RESOURCE_GROUP" ]]; then RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-}"; fi
if [[ -z "$CONTAINER_APP" || -z "$RESOURCE_GROUP" ]]; then
  if command -v azd >/dev/null 2>&1; then
    while IFS='=' read -r key value; do
      [[ -n "${key:-}" ]] || continue
      value="${value%\"}"; value="${value#\"}"
      case "$key" in
        CONTAINER_APP_NAME)    [[ -z "$CONTAINER_APP"  ]] && CONTAINER_APP="$value" ;;
        AZURE_RESOURCE_GROUP)  [[ -z "$RESOURCE_GROUP" ]] && RESOURCE_GROUP="$value" ;;
      esac
    done < <(azd env get-values 2>/dev/null || true)
  fi
fi
[[ -n "$CONTAINER_APP" && -n "$RESOURCE_GROUP" ]] \
  || die "need --container-app + --rg (or run from an azd env dir)"

# --- AKS cluster auto-detect -----------------------------------------------
if [[ -z "$CLUSTER_NAME" || -z "$CLUSTER_RG" ]]; then
  AKS_LIST="$(az aks list "${SUB_FLAG[@]}" \
    --query "[].{name:name, rg:resourceGroup}" -o tsv 2>/dev/null || true)"
  AKS_COUNT="$(printf '%s\n' "$AKS_LIST" | grep -c . || true)"
  if [[ "$AKS_COUNT" -eq 0 ]]; then
    die "no AKS cluster in subscription — nothing to peer."
  fi
  if [[ "$AKS_COUNT" -gt 1 && ( -z "$CLUSTER_NAME" || -z "$CLUSTER_RG" ) ]]; then
    red "multiple AKS clusters — be explicit with --cluster-name + --cluster-rg:"
    printf '%s\n' "$AKS_LIST" >&2
    exit 3
  fi
  AUTO_NAME="$(printf '%s\n' "$AKS_LIST" | awk '{print $1}')"
  AUTO_RG="$(printf '%s\n' "$AKS_LIST" | awk '{print $2}')"
  [[ -z "$CLUSTER_NAME" ]] && CLUSTER_NAME="$AUTO_NAME"
  [[ -z "$CLUSTER_RG" ]] && CLUSTER_RG="$AUTO_RG"
fi

# --- Resolve dashboard URL -------------------------------------------------
APP_FQDN="$(az containerapp show "${SUB_FLAG[@]}" \
  -n "$CONTAINER_APP" -g "$RESOURCE_GROUP" \
  --query "properties.configuration.ingress.fqdn" -o tsv 2>/dev/null || true)"
[[ -n "$APP_FQDN" ]] || die "Container App '$CONTAINER_APP' in '$RESOURCE_GROUP' not found or has no ingress"
DASH_URL="https://${APP_FQDN}/api/aks/peer-with-platform"

ts "Subscription:    $SUBSCRIPTION"
ts "Container App:   $CONTAINER_APP ($RESOURCE_GROUP)"
ts "AKS cluster:     $CLUSTER_NAME (rg=$CLUSTER_RG)"
ts "Endpoint:        POST $DASH_URL"

if [[ $ASSUME_YES -eq 0 ]]; then
  printf 'Peer dashboard VNet with %s? [y/N] ' "$CLUSTER_NAME"
  read -r ANS
  [[ "$ANS" == "y" || "$ANS" == "Y" ]] || die "aborted by user" 1
fi

if [[ $DRY_RUN -eq 1 ]]; then
  yellow "  [dry ] would POST to $DASH_URL with cluster_name=$CLUSTER_NAME resource_group=$CLUSTER_RG"
  exit 0
fi

# --- Get an Entra access token for the dashboard's API audience ------------
# The dashboard's `require_caller` validates the MSAL bearer token against
# its own App Registration. We use the SPA's `API_CLIENT_ID` audience.
API_CLIENT_ID="$(az containerapp show "${SUB_FLAG[@]}" \
  -n "$CONTAINER_APP" -g "$RESOURCE_GROUP" \
  --query "properties.template.containers[?name=='api'].env[?name=='API_CLIENT_ID'].value | [0] | [0]" \
  -o tsv 2>/dev/null || true)"
[[ -n "$API_CLIENT_ID" ]] || die "could not read API_CLIENT_ID from Container App 'api' sidecar env"

# Mirrors api.tasks.azure.peering._dashboard_vnet_id_from_env — used by the
# direct-az fallback when the dashboard endpoint is unreachable / token
# acquisition fails. The Container App template injects
# PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID on every sidecar; the parent VNet id
# is everything up to /subnets/<name>.
resolve_dashboard_vnet_id() {
  local subnet_id
  subnet_id="$(az containerapp show "${SUB_FLAG[@]}" \
    -n "$CONTAINER_APP" -g "$RESOURCE_GROUP" \
    --query "properties.template.containers[?name=='api'].env[?name=='PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID'].value | [0] | [0]" \
    -o tsv 2>/dev/null || true)"
  [[ -n "$subnet_id" ]] || return 1
  # /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Network/virtualNetworks/<name>/subnets/<subnet>
  printf '%s' "${subnet_id%/subnets/*}"
}

# Direct-az fallback that mirrors ensure_vnet_peering_with_cluster from
# api/tasks/azure/peering.py. Used when the dashboard endpoint cannot be
# reached for auth reasons (AADSTS65001 admin-consent required, missing
# SPA scope) — pure Network Contributor on both VNets is enough.
# Treats AlreadyExists / Conflict as success; bidirectional.
direct_az_peer() {
  local dash_vnet_id aks_node_rg aks_vnet_id dash_rg dash_name aks_rg aks_name
  dash_vnet_id="$(resolve_dashboard_vnet_id || true)"
  [[ -n "$dash_vnet_id" ]] \
    || { red "  [fall] direct az: could not resolve dashboard VNet from PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID"; return 3; }

  aks_node_rg="$(az aks show "${SUB_FLAG[@]}" -g "$CLUSTER_RG" -n "$CLUSTER_NAME" \
    --query nodeResourceGroup -o tsv 2>/dev/null || true)"
  [[ -n "$aks_node_rg" ]] \
    || { red "  [fall] direct az: could not read AKS nodeResourceGroup for $CLUSTER_NAME"; return 3; }

  aks_vnet_id="$(az network vnet list "${SUB_FLAG[@]}" -g "$aks_node_rg" \
    --query "[0].id" -o tsv 2>/dev/null || true)"
  [[ -n "$aks_vnet_id" ]] \
    || { yellow "  [fall] direct az: no VNet in $aks_node_rg (BYO-VNet mode?); nothing to peer"; return 0; }

  dash_rg="$(printf '%s' "$dash_vnet_id"   | awk -F'/' '{print $5}')"
  dash_name="$(printf '%s' "$dash_vnet_id" | awk -F'/' '{print $9}')"
  aks_rg="$(printf '%s' "$aks_vnet_id"     | awk -F'/' '{print $5}')"
  aks_name="$(printf '%s' "$aks_vnet_id"   | awk -F'/' '{print $9}')"

  ts "  [fall] direct az: peering $dash_name <-> $aks_name"

  local rc=0
  # dashboard -> aks
  if ! az network vnet peering create "${SUB_FLAG[@]}" \
      -g "$dash_rg" --vnet-name "$dash_name" \
      --name "peer-${dash_name}-to-${aks_name}" \
      --remote-vnet "$aks_vnet_id" \
      --allow-vnet-access \
      --query "peeringState" -o tsv 2>/tmp/peer-cluster-network.err >/dev/null; then
    if grep -qiE "AlreadyExists|Conflict" /tmp/peer-cluster-network.err 2>/dev/null; then
      yellow "  [fall] dashboard->aks already exists (idempotent)"
    else
      red "  [fall] dashboard->aks failed:"
      sed 's/^/    /' /tmp/peer-cluster-network.err
      rc=2
    fi
  else
    green "  [fall] dashboard->aks created"
  fi

  # aks -> dashboard
  if ! az network vnet peering create "${SUB_FLAG[@]}" \
      -g "$aks_rg" --vnet-name "$aks_name" \
      --name "peer-${aks_name}-to-${dash_name}" \
      --remote-vnet "$dash_vnet_id" \
      --allow-vnet-access \
      --query "peeringState" -o tsv 2>/tmp/peer-cluster-network.err >/dev/null; then
    if grep -qiE "AlreadyExists|Conflict" /tmp/peer-cluster-network.err 2>/dev/null; then
      yellow "  [fall] aks->dashboard already exists (idempotent)"
    else
      red "  [fall] aks->dashboard failed:"
      sed 's/^/    /' /tmp/peer-cluster-network.err
      rc=2
    fi
  else
    green "  [fall] aks->dashboard created"
  fi
  rm -f /tmp/peer-cluster-network.err
  return "$rc"
}

ACCESS_TOKEN="$(az account get-access-token \
  --resource "api://${API_CLIENT_ID}" \
  --query accessToken -o tsv 2>/dev/null || true)"
if [[ -z "$ACCESS_TOKEN" ]]; then
  yellow "could not get an access token for api://${API_CLIENT_ID}"
  yellow "(common when the SPA scope is not pre-consented — AADSTS65001)"
  yellow "falling back to direct az network vnet peering create"
  direct_az_peer
  rc=$?
  if [[ $rc -eq 0 ]]; then
    yellow "VNet peerings typically become Connected within 1-2 minutes."
  fi
  exit "$rc"
fi

PAYLOAD=$(printf '{"subscription_id":"%s","resource_group":"%s","cluster_name":"%s"}' \
  "$SUBSCRIPTION" "$CLUSTER_RG" "$CLUSTER_NAME")

HTTP_CODE="$(curl -sS -o /tmp/peer-cluster-network.body -w '%{http_code}' \
  -X POST "$DASH_URL" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data "$PAYLOAD" || echo 000)"

BODY="$(cat /tmp/peer-cluster-network.body 2>/dev/null || true)"
rm -f /tmp/peer-cluster-network.body

case "$HTTP_CODE" in
  200)
    green "  [ok  ] dashboard responded 200"
    printf '%s\n' "$BODY"
    if printf '%s' "$BODY" | grep -q '"error"'; then
      yellow "(one peering direction failed — see error field above)"
      exit 2
    fi
    ;;
  401|403)
    yellow "  [auth] dashboard returned $HTTP_CODE — token did not authorise the route"
    yellow "  falling back to direct az network vnet peering create"
    direct_az_peer
    exit $?
    ;;
  *)
    red "  [fail] dashboard returned HTTP $HTTP_CODE"
    printf '%s\n' "$BODY"
    yellow "  falling back to direct az network vnet peering create"
    direct_az_peer
    exit $?
    ;;
esac

yellow "VNet peerings typically become Connected within 1-2 minutes."
yellow "Verify with: az network vnet peering list --vnet-name <dashboard-vnet> --resource-group $RESOURCE_GROUP -o table"
