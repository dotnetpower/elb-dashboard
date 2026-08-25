#!/usr/bin/env bash
# storage-public-access.sh — open / close the workload Storage account's
# public network surface for LOCAL DEBUGGING ONLY.
#
# Why this exists
# ---------------
# Production keeps every Storage account `publicNetworkAccess: Disabled`
# and reaches the data plane via a private endpoint inside the platform
# VNet. From a developer laptop that is unreachable, so the BLAST
# Databases / Queries / Results screens render the "network_blocked"
# degraded state and you cannot exercise any code path that lists or
# reads blobs.
#
# Running this script with `on` flips the account to:
#
#   publicNetworkAccess = Enabled
#   networkAcls.defaultAction = Deny
#   networkAcls.bypass        = None
#   networkAcls.ipRules       = [<your caller IP>]
#
# i.e. the public data plane is reachable only from your current public IPv4.
# Entra ID auth is still enforced (allowSharedKeyAccess=false). Your `az login`
# identity must already hold `Storage Blob Data Reader` (or higher) on the
# account / container scope.
#
# Running with `off` reverts to the production posture
# (publicNetworkAccess = Disabled, ipRules cleared).
#
# This is intentionally a manual shell command, not a dashboard button —
# the friction is the safety mechanism. Do not check in any wrapper that
# calls this without explicit confirmation.
#
# Usage:
#   scripts/dev/storage-public-access.sh on  [--account NAME] [--rg NAME] [--ip IP] [--subscription ID]
#   scripts/dev/storage-public-access.sh off [--account NAME] [--rg NAME]                [--subscription ID]
#   scripts/dev/storage-public-access.sh status [--account NAME] [--rg NAME] [--subscription ID]
#
# Defaults: ACCOUNT=elbstg01, RG=rg-elb-01, IP=auto-detect via api.ipify.org,
#           SUBSCRIPTION=current `az account show`.

set -Eeuo pipefail

ACCOUNT_DEFAULT="elbstg01"
RG_DEFAULT="rg-elb-01"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
ts()     { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die()    { red "ERROR: $*" >&2; exit 1; }

usage() {
  sed -n '2,40p' "$0"
  exit "${1:-1}"
}

[[ $# -ge 1 ]] || usage 1
ACTION="$1"; shift || true
case "$ACTION" in
  on|off|status|-h|--help|help) ;;
  *) usage 1 ;;
esac
[[ "$ACTION" == "-h" || "$ACTION" == "--help" || "$ACTION" == "help" ]] && usage 0

ACCOUNT="$ACCOUNT_DEFAULT"
RG="$RG_DEFAULT"
IP=""
SUBSCRIPTION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account)      ACCOUNT="$2"; shift 2 ;;
    --rg)           RG="$2";      shift 2 ;;
    --ip)           IP="$2";      shift 2 ;;
    --subscription) SUBSCRIPTION="$2"; shift 2 ;;
    -h|--help)      usage 0 ;;
    *)              die "unknown flag: $1" ;;
  esac
done

command -v az >/dev/null 2>&1 || die "az CLI not found"
command -v jq >/dev/null 2>&1 || die "jq not found"
command -v curl >/dev/null 2>&1 || die "curl not found"

if [[ -n "${CONTAINER_APP_NAME:-}" && "$ACTION" != "status" ]]; then
  die "refusing to change Storage public access inside a Container App"
fi

is_ipv4() {
  local candidate="$1" octet
  local -a octets
  IFS=. read -r -a octets <<< "$candidate"
  [[ ${#octets[@]} -eq 4 ]] || return 1
  for octet in "${octets[@]}"; do
    [[ "$octet" =~ ^[0-9]{1,3}$ ]] || return 1
    (( 10#$octet <= 255 )) || return 1
  done
}

normalise_region() {
  printf '%s' "$1" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]'
}

azure_host_region() {
  curl -fsS --max-time 1 -H Metadata:true \
    'http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01' \
    2>/dev/null | jq -r '.location // empty' 2>/dev/null || true
}

# Resolve subscription.
if [[ -z "$SUBSCRIPTION" ]]; then
  SUBSCRIPTION="$(az account show --query id -o tsv 2>/dev/null || true)"
  [[ -n "$SUBSCRIPTION" ]] || die "no subscription set; run 'az login' or pass --subscription"
fi
SUB_FLAG=(--subscription "$SUBSCRIPTION")

# Confirm account exists in the resource group.
if ! az storage account show "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" -o none 2>/dev/null; then
  die "storage account '$ACCOUNT' not found in resource group '$RG' (subscription $SUBSCRIPTION)"
fi

print_state() {
  local payload
  payload="$(az storage account show "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" \
      --query '{public:publicNetworkAccess,defaultAction:networkRuleSet.defaultAction,ipRules:networkRuleSet.ipRules,bypass:networkRuleSet.bypass}' \
      -o json)"
  echo "  account:       $ACCOUNT"
  echo "  resourceGroup: $RG"
  echo "  subscription:  $SUBSCRIPTION"
  echo "  current state: $(echo "$payload" | jq -c .)"
}

case "$ACTION" in
  status)
    ts "Current network state of '$ACCOUNT':"
    print_state
    exit 0
    ;;

  on)
    if [[ -z "$IP" ]]; then
      ts "Detecting caller public IP via api.ipify.org ..."
      IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
      [[ -n "$IP" ]] || die "could not auto-detect IP; pass --ip <your-public-ip>"
    fi
    is_ipv4 "$IP" || die "IP '$IP' is not a bare IPv4 address"

    storage_region="$(az storage account show "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" \
      --query primaryLocation -o tsv)"
    host_region="$(azure_host_region)"
    if [[ -n "$host_region" \
       && "$(normalise_region "$host_region")" == "$(normalise_region "$storage_region")" ]]; then
      die "Azure Storage IP rules do not apply to same-region Azure clients ($host_region). Use the deployed private-endpoint path or an approved virtual-network rule; Storage was not changed."
    fi

    ts "Opening '$ACCOUNT' for caller IP $IP (defaultAction=Deny, bypass=None) ..."
    # Close first so remediation from an unsafe state can never leave an
    # Enabled+Allow interval while rules are replaced.
    az storage account update "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" \
        --public-network-access Disabled --default-action Deny --bypass None -o none

    # Replace stale local-debug entries rather than accumulating trusted IPs.
    existing_ips="$(az storage account network-rule list "${SUB_FLAG[@]}" -g "$RG" --account-name "$ACCOUNT" \
        --query 'ipRules[].ipAddressOrRange' -o tsv 2>/dev/null || true)"
    if [[ -n "$existing_ips" ]]; then
      while IFS= read -r prev_ip; do
        [[ -z "$prev_ip" ]] && continue
        az storage account network-rule remove "${SUB_FLAG[@]}" -g "$RG" --account-name "$ACCOUNT" \
            --ip-address "$prev_ip" -o none
      done <<< "$existing_ips"
    fi
    az storage account network-rule add "${SUB_FLAG[@]}" -g "$RG" --account-name "$ACCOUNT" \
        --ip-address "$IP" -o none

    # Enable public access only after the deny-by-default rule set is complete.
    az storage account update "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" \
        --public-network-access Enabled --default-action Deny --bypass None -o none

    ts "Waiting ~90 s for the firewall change to propagate ..."
    sleep 90

    state="$(az storage account show "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" \
      --query '{public:publicNetworkAccess,defaultAction:networkRuleSet.defaultAction,bypass:networkRuleSet.bypass,ipRules:networkRuleSet.ipRules[].ipAddressOrRange}' \
      -o json)"
    if [[ "$(echo "$state" | jq -r '.public')" != "Enabled" \
       || "$(echo "$state" | jq -r '.defaultAction')" != "Deny" \
       || "$(echo "$state" | jq -r '.bypass')" != "None" \
       || "$(echo "$state" | jq -r '.ipRules | length')" -ne 1 \
       || "$(echo "$state" | jq -r '.ipRules[0]')" != "$IP" ]]; then
      die "failed to establish caller-IP-only Storage access; state=$(echo "$state" | jq -c .)"
    fi

    green "OPEN — storage account '$ACCOUNT' now accepts data-plane traffic from $IP"
    print_state

    cat <<EOF

Reminder:
  * RBAC is unchanged. Your az login identity must already hold
      'Storage Blob Data Reader'  (read-only views)
      'Storage Blob Data Contributor' (uploads / writes)
    on $ACCOUNT (or one of its containers).
  * Network access is limited to $IP. Close the surface as soon as you are done:
      $0 off --account $ACCOUNT --rg $RG
EOF
    ;;

  off)
    ts "Closing '$ACCOUNT' (publicNetworkAccess=Disabled, ipRules cleared) ..."
    # Close first. Cleanup failures after this point cannot leave public access enabled.
    az storage account update "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" \
      --public-network-access Disabled --default-action Deny --bypass None -o none

    existing_ips="$(az storage account network-rule list "${SUB_FLAG[@]}" -g "$RG" --account-name "$ACCOUNT" \
        --query 'ipRules[].ipAddressOrRange' -o tsv 2>/dev/null || true)"
    if [[ -n "$existing_ips" ]]; then
      while IFS= read -r prev_ip; do
        [[ -z "$prev_ip" ]] && continue
        az storage account network-rule remove "${SUB_FLAG[@]}" -g "$RG" --account-name "$ACCOUNT" \
            --ip-address "$prev_ip" -o none
      done <<< "$existing_ips"
    fi

    state="$(az storage account show "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" \
      --query '{public:publicNetworkAccess,defaultAction:networkRuleSet.defaultAction,bypass:networkRuleSet.bypass,ipRules:networkRuleSet.ipRules}' \
      -o json)"
    public_state="$(echo "$state" | jq -r '.public')"
    default_action="$(echo "$state" | jq -r '.defaultAction')"
    bypass="$(echo "$state" | jq -r '.bypass')"
    ip_rule_count="$(echo "$state" | jq -r '.ipRules | length')"
    if [[ "$public_state" != "Disabled" || "$default_action" != "Deny" \
       || "$bypass" != "None" || "$ip_rule_count" -ne 0 ]]; then
      die "failed to close storage network; state=$(echo "$state" | jq -c .)"
    fi

    green "CLOSED — storage account '$ACCOUNT' is back to publicNetworkAccess=Disabled"
    print_state
    ;;
esac
