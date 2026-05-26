#!/usr/bin/env bash
# grant-runtime-rbac.sh — grant the deployed elb-dashboard's user-assigned
# managed identity the runtime roles it needs to orchestrate AKS-side
# workloads (OpenAPI deploy, kubectl apply, BLAST submit federated identity
# setup) from inside the workload cluster RG.
#
# Why this exists
# ---------------
# `infra/modules/controlPlaneRoles.bicep` grants the shared dashboard MI
# (`id-elb-dashboard-*`) Contributor + User Access Administrator on the
# *deployment* RG only. The OpenAPI deploy task at
# `api/tasks/openapi/rbac.py::setup_workload_identity` reaches into the
# **AKS cluster's RG** (typically `rg-elb-cluster`) and tries to:
#
#   1. Create the workload MI `id-elb-openapi`            (needs `Microsoft.ManagedIdentity/userAssignedIdentities/write`)
#   2. Create a Federated Identity Credential under it    (same write permission)
#   3. Assign Contributor / Storage Blob Data Contributor / AKS Cluster User
#      to that MI                                         (needs `Microsoft.Authorization/roleAssignments/write` = UAA)
#   4. `az aks get-credentials --admin` (via terminal sidecar) and
#      `kubectl apply` the OpenAPI manifests              (needs `listClusterAdminCredential/action` = Contributor)
#
# Without those roles the user clicks "Deploy elb-openapi" and the SPA
# shows: "workload identity setup failed; OpenAPI pod would have no AZURE_CLIENT_ID."
#
# This script closes that gap by granting on the AKS cluster RG:
#   - Contributor                  (MI + federated cred CRUD, AKS cluster read/get-credentials)
#   - User Access Administrator    (role assignment writes inside that RG and on the cluster itself)
#
# Roles are NEVER revoked — to remove them later use
#   az role assignment delete --assignee <oid> --role <name> --scope <id>
#
# This script changes only RBAC. It does NOT touch the Container App, MI,
# AKS cluster, Storage, ACR, or any network surface.
#
# Usage:
#   scripts/dev/grant-runtime-rbac.sh                       # auto-detect from azd env + AKS list
#   scripts/dev/grant-runtime-rbac.sh --container-app ca-elb-dashboard --rg rg-elb-dashboard
#   scripts/dev/grant-runtime-rbac.sh --principal-id <oid> --cluster-rg rg-elb-cluster
#   scripts/dev/grant-runtime-rbac.sh --cluster-rg rg-elb-cluster --region koreacentral
#                                                            # bootstrap mode: create the RG
#                                                            # if it doesn't exist yet, then grant.
#   scripts/dev/grant-runtime-rbac.sh --dry-run
#   scripts/dev/grant-runtime-rbac.sh --yes                 # skip the "proceed?" prompt
#
# Auto-detection rules:
#   * Dashboard MI principalId — from the Container App's
#     `identity.userAssignedIdentities` (the SINGLE shared MI).
#   * AKS cluster RG — when the subscription has exactly one AKS cluster,
#     use its RG. When there are multiple, refuse and ask for --cluster-rg.
#     When there are zero clusters AND --cluster-rg was passed, run in
#     bootstrap mode (create the RG if --region is also passed, then grant).
#   * Container App name + RG — from `azd env get-values` keys
#     `CONTAINER_APP_NAME` and `AZURE_RESOURCE_GROUP`.
#
# Bootstrap mode (first-time cluster create on a fresh subscription):
#   The shared dashboard MI is granted only Reader at subscription scope,
#   so `api.tasks.azure.provision.provision_aks` -> `rc.resource_groups
#   .create_or_update(<cluster_rg>, ...)` returns AuthorizationFailed for
#   `Microsoft.Resources/subscriptions/resourcegroups/write` when the RG
#   doesn't already exist. To unblock that path safely (without granting
#   Contributor at subscription scope) the caller pre-creates the RG and
#   then grants Contributor + UAA on that RG only. Pass --cluster-rg with
#   --region to do both in one shot.
#
# Exit codes:
#   0  every requested role was already present or newly granted
#   2  one or more role grants failed (caller lacks UAA at the target scope)
#   3  preconditions not met (az not logged in, MI not found, ambiguous AKS,
#      RG missing without --region in bootstrap mode)

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
ts()     { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die()    { red "ERROR: $*" >&2; exit "${2:-3}"; }

usage() {
  # Print the leading comment block (header through "Exit codes"). Keep this
  # range in sync with the header docs above; we slice to the final exit-code
  # line so bootstrap-mode usage stays discoverable from --help.
  sed -n '2,69p' "$0"
  exit "${1:-1}"
}

CONTAINER_APP=""
RESOURCE_GROUP=""
SUBSCRIPTION=""
PRINCIPAL_ID=""
CLUSTER_RG=""
CLUSTER_REGION=""
DRY_RUN=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container-app|--app)   CONTAINER_APP="$2";   shift 2 ;;
    --rg|--resource-group)   RESOURCE_GROUP="$2";  shift 2 ;;
    --subscription)          SUBSCRIPTION="$2";    shift 2 ;;
    --principal-id|--oid)    PRINCIPAL_ID="$2";    shift 2 ;;
    --cluster-rg)            CLUSTER_RG="$2";      shift 2 ;;
    --region|--location)     CLUSTER_REGION="$2";  shift 2 ;;
    --dry-run)               DRY_RUN=1;            shift ;;
    --yes|-y)                ASSUME_YES=1;         shift ;;
    -h|--help)               usage 0 ;;
    *)                       die "unknown flag: $1" ;;
  esac
done

command -v az >/dev/null 2>&1 || die "az CLI not found"

# ---------------------------------------------------------------------------
# Auto-detect subscription + Container App + RG from azd env / az current.
# ---------------------------------------------------------------------------
if [[ -z "$SUBSCRIPTION" ]]; then
  SUBSCRIPTION="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv 2>/dev/null || true)}"
  [[ -n "$SUBSCRIPTION" ]] || die "no subscription set; run 'az login' or pass --subscription"
fi
SUB_FLAG=(--subscription "$SUBSCRIPTION")
az account set "${SUB_FLAG[@]}" >/dev/null 2>&1 || die "could not set subscription $SUBSCRIPTION"

# Pull missing CONTAINER_APP_NAME / AZURE_RESOURCE_GROUP from azd env when
# they aren't on the command line and aren't already exported (cli-upgrade
# already does this; mirror the rules so the script also works standalone).
if [[ -z "$CONTAINER_APP" ]]; then
  CONTAINER_APP="${CONTAINER_APP_NAME:-}"
fi
if [[ -z "$RESOURCE_GROUP" ]]; then
  RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-}"
fi
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

# ---------------------------------------------------------------------------
# Resolve the dashboard MI principal id (the principal that will receive
# the grants). Either passed in via --principal-id or read from the
# Container App's `identity.userAssignedIdentities`.
# ---------------------------------------------------------------------------
if [[ -z "$PRINCIPAL_ID" ]]; then
  [[ -n "$CONTAINER_APP" && -n "$RESOURCE_GROUP" ]] \
    || die "need --principal-id OR (--container-app + --rg) to find the MI"
  IDENTITY_JSON="$(az containerapp show "${SUB_FLAG[@]}" \
    -n "$CONTAINER_APP" -g "$RESOURCE_GROUP" \
    --query "identity" -o json 2>/dev/null || true)"
  [[ -n "$IDENTITY_JSON" && "$IDENTITY_JSON" != "null" ]] \
    || die "Container App '$CONTAINER_APP' in '$RESOURCE_GROUP' not found or has no identity"

  # Pick the single user-assigned MI. We deliberately reject multi-UAMI
  # apps here — the deploy contract assumes one shared MI.
  PRINCIPAL_ID="$(printf '%s' "$IDENTITY_JSON" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); ids=list((d.get("userAssignedIdentities") or {}).values()); print(ids[0]["principalId"] if len(ids)==1 else "")' 2>/dev/null || true)"
  [[ -n "$PRINCIPAL_ID" ]] \
    || die "Container App '$CONTAINER_APP' has zero or multiple user-assigned MIs; pass --principal-id explicitly"
fi
[[ "$PRINCIPAL_ID" =~ ^[0-9a-f-]{36}$ ]] \
  || die "principal-id '$PRINCIPAL_ID' does not look like an Entra object id (GUID)"

# ---------------------------------------------------------------------------
# Resolve the AKS cluster RG. If --cluster-rg was passed, use it as-is.
# Otherwise list AKS in the subscription and accept only the unambiguous
# "exactly one" case. When no AKS clusters exist AND --cluster-rg was
# not passed, print an actionable bootstrap hint instead of silently
# returning 0 (which would hide the first-time-cluster-create RBAC gap).
# ---------------------------------------------------------------------------
BOOTSTRAP_MODE=0
if [[ -z "$CLUSTER_RG" ]]; then
  AKS_LIST="$(az aks list "${SUB_FLAG[@]}" \
    --query "[].{name:name, rg:resourceGroup}" -o tsv 2>/dev/null || true)"
  AKS_COUNT="$(printf '%s\n' "$AKS_LIST" | grep -c . || true)"
  if [[ "$AKS_COUNT" -eq 0 ]]; then
    yellow "no AKS cluster found in subscription — nothing to grant in maintenance mode."
    yellow "If you plan to create a NEW cluster via the SPA, the dashboard MI must have"
    yellow "Contributor on the cluster RG so 'create_or_update(<cluster_rg>)' can succeed."
    yellow "Re-run this script in bootstrap mode (creates RG + grants):"
    yellow "  bash scripts/dev/grant-runtime-rbac.sh \\"
    yellow "    --cluster-rg <rg-elb-cluster> --region <koreacentral> --yes"
    exit 0
  fi
  if [[ "$AKS_COUNT" -gt 1 ]]; then
    red "multiple AKS clusters found in subscription — be explicit with --cluster-rg:"
    printf '%s\n' "$AKS_LIST" >&2
    exit 3
  fi
  CLUSTER_RG="$(printf '%s\n' "$AKS_LIST" | awk '{print $2}')"
fi

# Bootstrap path: --cluster-rg was specified (explicitly or auto-derived) but
# the RG doesn't exist yet. We do NOT die anymore — that was exactly the gap
# that left fresh-subscription deploys with no way to grant the MI write
# access to the future cluster RG without escalating to subscription-scope
# Contributor. Two sub-cases:
#   (a) caller passed --region — create the RG, then continue with the grant.
#   (b) caller did NOT pass --region — actionable die() telling them how to
#       fix it; we deliberately do not pick a region for them.
if ! az group show "${SUB_FLAG[@]}" -n "$CLUSTER_RG" -o none 2>/dev/null; then
  if [[ -z "$CLUSTER_REGION" ]]; then
    red "AKS cluster RG '$CLUSTER_RG' does not exist yet."
    red "Re-run with --region <azure-region> to create it before the grant, e.g.:"
    red "  bash scripts/dev/grant-runtime-rbac.sh \\"
    red "    --cluster-rg $CLUSTER_RG --region koreacentral --yes"
    exit 3
  fi
  BOOTSTRAP_MODE=1
  ts "Bootstrap mode: RG '$CLUSTER_RG' missing — will create in $CLUSTER_REGION."
  if [[ $DRY_RUN -eq 1 ]]; then
    yellow "  [dry ] would run: az group create -n $CLUSTER_RG -l $CLUSTER_REGION"
  else
    az group create "${SUB_FLAG[@]}" \
      --name "$CLUSTER_RG" --location "$CLUSTER_REGION" --output none \
      || die "failed to create resource group '$CLUSTER_RG' in '$CLUSTER_REGION'" 2
    ts "  [ok  ] created resource group $CLUSTER_RG in $CLUSTER_REGION"
  fi
fi

CLUSTER_RG_SCOPE="/subscriptions/${SUBSCRIPTION}/resourceGroups/${CLUSTER_RG}"

# ---------------------------------------------------------------------------
# Plan summary.
# ---------------------------------------------------------------------------
ts "Subscription:    $SUBSCRIPTION"
ts "Container App:   ${CONTAINER_APP:-<not used>} (${RESOURCE_GROUP:-<not used>})"
ts "Dashboard MI:    $PRINCIPAL_ID"
ts "AKS cluster RG:  $CLUSTER_RG${BOOTSTRAP_MODE:+ (bootstrap)}"
[[ $DRY_RUN -eq 1 ]] && yellow "(dry-run — no role assignments will be created)"

if [[ $ASSUME_YES -ne 1 && $DRY_RUN -ne 1 ]]; then
  printf 'Grant Contributor + User Access Administrator on %s to %s? [y/N] ' \
    "$CLUSTER_RG_SCOPE" "$PRINCIPAL_ID"
  read -r ANS
  [[ "$ANS" == "y" || "$ANS" == "Y" ]] || die "aborted by user" 1
fi

# (role-name, scope) pairs.
ASSIGNMENTS=(
  "Contributor|$CLUSTER_RG_SCOPE"
  "User Access Administrator|$CLUSTER_RG_SCOPE"
)

CREATED=0
SKIPPED=0
FAILED=0

for entry in "${ASSIGNMENTS[@]}"; do
  ROLE="${entry%%|*}"
  SCOPE="${entry##*|}"

  EXISTING="$(az role assignment list "${SUB_FLAG[@]}" \
      --assignee-object-id "$PRINCIPAL_ID" \
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
        --assignee-object-id "$PRINCIPAL_ID" \
        --assignee-principal-type ServicePrincipal \
        --role "$ROLE" \
        --scope "$SCOPE" \
        --output none 2>/tmp/grant-runtime-rbac.err; then
    ts "  [ok  ] granted $ROLE at $SCOPE"
    CREATED=$((CREATED + 1))
  else
    red "  [fail] could not grant $ROLE at $SCOPE — $(tr -d '\n' </tmp/grant-runtime-rbac.err | head -c 240)"
    FAILED=$((FAILED + 1))
  fi
done

echo
ts "Summary: created=$CREATED skipped=$SKIPPED failed=$FAILED"
if [[ $CREATED -gt 0 ]]; then
  yellow "RBAC propagation usually takes 1-5 minutes. The on-cluster OpenAPI"
  yellow "deploy will start succeeding once Azure replicates the role assignments."
fi
[[ $FAILED -eq 0 ]] || exit 2
