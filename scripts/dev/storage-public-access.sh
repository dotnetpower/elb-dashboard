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
#   networkAcls.defaultAction = Allow
#   networkAcls.bypass        = AzureServices
#
# i.e. the data plane is reachable from any IP, but Entra ID auth is still
# enforced (allowSharedKeyAccess=false). Your `az login` identity must already
# hold `Storage Blob Data Reader` (or higher) on the account / container scope.
#
# Why not Deny + ipRule? For ADLS Gen2 (isHnsEnabled=true) accounts with an
# approved private endpoint, defaultAction=Deny + ipRule does not reliably
# propagate to the data plane. defaultAction=Allow is the only method that
# works reliably for local development access. See docs/features_change/ for
# the root-cause analysis.
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
    ts "Opening '$ACCOUNT' for local debugging (publicNetworkAccess=Enabled, defaultAction=Allow) ..."
    # 1. Enable the public surface.
    az storage account update "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" \
        --public-network-access Enabled -o none
    # 2. Set defaultAction=Allow with bypass=AzureServices.
    #    Note: defaultAction=Deny + ipRule does NOT reliably propagate to the
    #    data plane for ADLS Gen2 (isHnsEnabled=true) accounts with a private
    #    endpoint. defaultAction=Allow is used instead. Azure AD auth
    #    (allowSharedKeyAccess=false) is still enforced on every request.
    az storage account update "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" \
        --default-action Allow --bypass AzureServices -o none

    ts "Waiting ~90 s for the firewall change to propagate ..."
    sleep 90

    green "OPEN — storage account '$ACCOUNT' now accepts data-plane traffic (defaultAction=Allow)"
    print_state

    cat <<EOF

Reminder:
  * RBAC is unchanged. Your az login identity must already hold
      'Storage Blob Data Reader'  (read-only views)
      'Storage Blob Data Contributor' (uploads / writes)
    on $ACCOUNT (or one of its containers).
  * defaultAction=Allow means any authenticated Azure AD identity can reach
    the data plane. Close the surface as soon as you are done:
      $0 off --account $ACCOUNT --rg $RG
EOF
    ;;

  off)
    ts "Closing '$ACCOUNT' (publicNetworkAccess=Disabled, ipRules cleared) ..."
    # Wipe the IP allowlist first so a future `on` starts clean.
    existing_ips="$(az storage account network-rule list "${SUB_FLAG[@]}" -g "$RG" --account-name "$ACCOUNT" \
        --query 'ipRules[].ipAddressOrRange' -o tsv 2>/dev/null || true)"
    if [[ -n "$existing_ips" ]]; then
      while IFS= read -r prev_ip; do
        [[ -z "$prev_ip" ]] && continue
        az storage account network-rule remove "${SUB_FLAG[@]}" -g "$RG" --account-name "$ACCOUNT" \
            --ip-address "$prev_ip" -o none 2>/dev/null || true
      done <<< "$existing_ips"
    fi
    # Restore the production defaultAction (Deny) and disable the public surface.
    az storage account update "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" \
        --default-action Deny -o none
    az storage account update "${SUB_FLAG[@]}" -g "$RG" -n "$ACCOUNT" \
        --public-network-access Disabled -o none

    green "CLOSED — storage account '$ACCOUNT' is back to publicNetworkAccess=Disabled"
    print_state
    ;;
esac
