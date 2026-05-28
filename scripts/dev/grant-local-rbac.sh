#!/usr/bin/env bash
# grant-local-rbac.sh — grant the locally signed-in az user the minimum
# RBAC roles needed to drive a deployed elb-dashboard from a developer
# laptop (api running on 127.0.0.1:8085).
#
# Why this exists
# ---------------
# In production the api / worker sidecars use the user-assigned MI
# `id-elb-dashboard-*` (granted via Bicep + docs/auth.md). When the api runs
# locally under `uv run uvicorn ...`, `DefaultAzureCredential` falls back
# to your `az login` identity instead — and that identity starts with
# zero RBAC on the workload Storage / ACR / RG, so the dashboard renders
# the "network_blocked" / "access_denied" degraded state and DB downloads
# fail with HTTP 403 AuthorizationPermissionMismatch.
#
# This script grants the signed-in user the minimum role set needed for
# local development:
#
#   on workload Storage account (e.g. elbstg01):
#     - Storage Blob Data Contributor    (data plane: copy DBs, read/list blobs)
#     - Storage Table Data Contributor   (data plane: jobstate / jobhistory rows)
#     - Storage Account Contributor      (control plane: lets the local-debug
#                                         auto-open helper toggle publicNetworkAccess
#                                         + ipRules — see api/services/storage/public_access.py)
#
#   on workload Storage RG (e.g. rg-elb-01):
#     - Reader                           (so /api/monitor/* can list AKS / storage / etc.)
#
#   on workload ACR (e.g. elbacr01):
#     - AcrPull                          (so the dashboard ACR card can list repositories + tags)
#
# Roles are NEVER revoked — to remove them later use
#   az role assignment delete --assignee <oid> --role <name> --scope <id>
#
# This script does NOT change any network surface (use storage-public-access.sh
# for that) and does NOT touch the Container App / Managed Identity in any way.
#
# Usage:
#   scripts/dev/grant-local-rbac.sh                 # use defaults below
#   scripts/dev/grant-local-rbac.sh --storage elbstg01 --storage-rg rg-elb-01 \
#                                   --acr elbacr01  --acr-rg rg-elbacr-01
#   scripts/dev/grant-local-rbac.sh --subscription <sub-id>
#   scripts/dev/grant-local-rbac.sh --user someone@contoso.onmicrosoft.com   # grant to another user
#   scripts/dev/grant-local-rbac.sh --dry-run
#
# Defaults match the example deployment in docs/auth.md.

set -Eeuo pipefail

# Documentation example fallback (matches docs/auth.md). Real values come
# from `azd env get-values` whenever the caller does not pass explicit flags,
# so a fresh `azd up` deployment with auto-suffixed names (stelbdashboard…,
# acrelbdashboard…, rg-elb-dashboard) Just Works.
STORAGE_DEFAULT="elbstg01"
STORAGE_RG_DEFAULT="rg-elb-01"
ACR_DEFAULT="elbacr01"
ACR_RG_DEFAULT="rg-elbacr-01"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
ts()     { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die()    { red "ERROR: $*" >&2; exit 1; }

usage() {
  sed -n '2,55p' "$0"
  exit "${1:-1}"
}

STORAGE=""
STORAGE_RG=""
ACR=""
ACR_RG=""
SUBSCRIPTION=""
USER_ID=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --storage)      STORAGE="$2";       shift 2 ;;
    --storage-rg)   STORAGE_RG="$2";    shift 2 ;;
    --acr)          ACR="$2";           shift 2 ;;
    --acr-rg)       ACR_RG="$2";        shift 2 ;;
    --subscription) SUBSCRIPTION="$2";  shift 2 ;;
    --user)         USER_ID="$2";       shift 2 ;;
    --dry-run)      DRY_RUN=1;          shift   ;;
    -h|--help)      usage 0 ;;
    *)              die "unknown flag: $1" ;;
  esac
done

command -v az >/dev/null 2>&1 || die "az CLI not found"

# Resolve subscription.
if [[ -z "$SUBSCRIPTION" ]]; then
  SUBSCRIPTION="$(az account show --query id -o tsv 2>/dev/null || true)"
  [[ -n "$SUBSCRIPTION" ]] || die "no subscription set; run 'az login' or pass --subscription"
fi
SUB_FLAG=(--subscription "$SUBSCRIPTION")

# Auto-fill any unset names from `azd env get-values` (real deployment
# values), then fall back to the docs example defaults. Mirrors the
# resolution flow in local-debug-auth.sh.
if command -v azd >/dev/null 2>&1; then
  azd_values="$(azd env get-values 2>/dev/null || true)"
  if [[ -n "$azd_values" ]]; then
    azd_kv() {
      awk -F= -v key="$1" '$1==key {gsub(/"/,"",$2); print $2; exit}' <<<"$azd_values"
    }
    [[ -z "$STORAGE" ]]    && STORAGE="$(azd_kv STORAGE_ACCOUNT_NAME)"
    [[ -z "$STORAGE_RG" ]] && STORAGE_RG="$(azd_kv AZURE_RESOURCE_GROUP)"
    [[ -z "$ACR" ]]        && ACR="$(azd_kv ACR_NAME)"
    [[ -z "$ACR_RG" ]]     && ACR_RG="$STORAGE_RG"
  fi
fi
[[ -z "$STORAGE" ]]    && STORAGE="$STORAGE_DEFAULT"
[[ -z "$STORAGE_RG" ]] && STORAGE_RG="$STORAGE_RG_DEFAULT"
[[ -z "$ACR" ]]        && ACR="$ACR_DEFAULT"
[[ -z "$ACR_RG" ]]     && ACR_RG="$ACR_RG_DEFAULT"

# Final fallback: if the chosen storage name does not exist, try to find
# exactly one stelbdashboard* account in the subscription and use it.
if ! az storage account show "${SUB_FLAG[@]}" -g "$STORAGE_RG" -n "$STORAGE" -o none 2>/dev/null; then
  matches="$(az storage account list "${SUB_FLAG[@]}" \
      --query "[?starts_with(name,'stelbdashboard')].{name:name,rg:resourceGroup}" -o tsv 2>/dev/null || true)"
  count="$(printf '%s\n' "$matches" | grep -c . || true)"
  if [[ "$count" == "1" ]]; then
    STORAGE="$(awk '{print $1}' <<<"$matches")"
    STORAGE_RG="$(awk '{print $2}' <<<"$matches")"
    yellow "auto-resolved storage: $STORAGE (rg: $STORAGE_RG)"
  fi
fi

# Same fallback for ACR — pick the single acrelbdashboard* registry if the
# named one is missing (azd env may be out of date after a redeploy).
if ! az acr show "${SUB_FLAG[@]}" -g "$ACR_RG" -n "$ACR" -o none 2>/dev/null; then
  acr_matches="$(az acr list "${SUB_FLAG[@]}" \
      --query "[?starts_with(name,'acrelbdashboard')].{name:name,rg:resourceGroup}" -o tsv 2>/dev/null || true)"
  acr_count="$(printf '%s\n' "$acr_matches" | grep -c . || true)"
  if [[ "$acr_count" == "1" ]]; then
    ACR="$(awk '{print $1}' <<<"$acr_matches")"
    ACR_RG="$(awk '{print $2}' <<<"$acr_matches")"
    yellow "auto-resolved acr: $ACR (rg: $ACR_RG)"
  fi
fi

# Resolve principal we are granting to.
if [[ -z "$USER_ID" ]]; then
  USER_ID="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
  USER_NAME="$(az account show --query user.name -o tsv 2>/dev/null || echo '<unknown>')"
  [[ -n "$USER_ID" ]] || die "could not resolve signed-in user; run 'az login' first"
else
  # Resolve email / UPN to objectId if needed.
  if [[ ! "$USER_ID" =~ ^[0-9a-f-]{36}$ ]]; then
    RESOLVED="$(az ad user show --id "$USER_ID" --query id -o tsv 2>/dev/null || true)"
    [[ -n "$RESOLVED" ]] || die "could not resolve user '$USER_ID' to an objectId"
    USER_NAME="$USER_ID"
    USER_ID="$RESOLVED"
  else
    USER_NAME="$USER_ID"
  fi
fi

ts "Subscription: $SUBSCRIPTION"
ts "Principal:    $USER_NAME ($USER_ID)"
ts "Storage:      $STORAGE in $STORAGE_RG"
ts "ACR:          $ACR in $ACR_RG"
[[ $DRY_RUN -eq 1 ]] && yellow "(dry-run — no role assignments will be created)"
echo

# Resolve target scopes.
STORAGE_SCOPE="$(az storage account show "${SUB_FLAG[@]}" -g "$STORAGE_RG" -n "$STORAGE" --query id -o tsv 2>/dev/null || true)"
[[ -n "$STORAGE_SCOPE" ]] || die "storage account '$STORAGE' not found in '$STORAGE_RG'"

STORAGE_RG_SCOPE="$(az group show "${SUB_FLAG[@]}" -n "$STORAGE_RG" --query id -o tsv 2>/dev/null || true)"
[[ -n "$STORAGE_RG_SCOPE" ]] || die "resource group '$STORAGE_RG' not found"

ACR_SCOPE="$(az acr show "${SUB_FLAG[@]}" -g "$ACR_RG" -n "$ACR" --query id -o tsv 2>/dev/null || true)"
if [[ -z "$ACR_SCOPE" ]]; then
  yellow "WARN: ACR '$ACR' not found in '$ACR_RG' — skipping AcrPull assignment"
fi

# (role-name, scope) pairs to apply.
ASSIGNMENTS=(
  "Storage Blob Data Contributor|$STORAGE_SCOPE"
  "Storage Table Data Contributor|$STORAGE_SCOPE"
  "Storage Account Contributor|$STORAGE_SCOPE"
  "Reader|$STORAGE_RG_SCOPE"
)
[[ -n "$ACR_SCOPE" ]] && ASSIGNMENTS+=("AcrPull|$ACR_SCOPE")

CREATED=0
SKIPPED=0
FAILED=0

for entry in "${ASSIGNMENTS[@]}"; do
  ROLE="${entry%%|*}"
  SCOPE="${entry##*|}"

  EXISTING="$(az role assignment list "${SUB_FLAG[@]}" \
      --assignee-object-id "$USER_ID" \
      --role "$ROLE" \
      --scope "$SCOPE" \
      --query '[0].id' -o tsv 2>/dev/null || true)"

  if [[ -n "$EXISTING" ]]; then
    green "  [skip] $ROLE already assigned at $SCOPE"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    yellow "  [dry ] would assign $ROLE at $SCOPE"
    continue
  fi

  if az role assignment create "${SUB_FLAG[@]}" \
        --assignee-object-id "$USER_ID" \
        --assignee-principal-type User \
        --role "$ROLE" \
        --scope "$SCOPE" \
        --output none 2>/tmp/grant-local-rbac.err; then
    ts "  [ok  ] granted $ROLE at $SCOPE"
    CREATED=$((CREATED + 1))
  else
    red "  [fail] could not grant $ROLE at $SCOPE — $(tr -d '\n' </tmp/grant-local-rbac.err | head -c 240)"
    FAILED=$((FAILED + 1))
  fi
done

echo
ts "Summary: created=$CREATED skipped=$SKIPPED failed=$FAILED"
if [[ $CREATED -gt 0 ]]; then
  yellow "RBAC propagation usually takes 1-5 minutes. If the dashboard still"
  yellow "shows 'access_denied' after that, sign out and back in:"
  echo  "  az logout && az login"
fi
[[ $FAILED -eq 0 ]] || exit 2
