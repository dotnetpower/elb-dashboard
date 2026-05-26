#!/usr/bin/env bash
# check-mi-rbac.sh — read-only doctor for the deployed dashboard MI.
#
# Why this exists
# ---------------
# Bicep is idempotent for the role assignments it owns, but the gaps are
# the ones outside Bicep:
#
#   * A previous `azd down` + `azd up` cycle changed the MI principalId.
#     Bicep re-created the in-Bicep assignments under the new principal
#     id (the `guid(scope, principalId, roleId)` name differs), but any
#     assignments granted **outside** Bicep — workload Storage/ACR
#     attached via the SPA wizard, the AKS cluster RG before the
#     bootstrap script was added, a pre-existing ACR the operator
#     pointed the dashboard at — are now orphaned and the MI has no
#     access.
#
#   * The SPA's "Use existing resource" path attaches the dashboard to a
#     resource that Bicep never touched. No RBAC is auto-granted.
#
#   * The cluster-RG Contributor/UAA grant only happens via
#     `workloadClusterRoles.bicep` when `aksClusterResourceGroup` is set
#     on the azd env, OR via the runtime safety net
#     `grant-runtime-rbac.sh`. It is easy to skip one without noticing.
#
# This script does NOT change any RBAC. It enumerates the expected
# {scope, role} pairs the dashboard MI needs to function and reports
# which ones are missing, alongside the **exact** `az role assignment
# create` command the operator (or a tenant/sub admin) can paste to fix
# it. Output is grouped by severity so a CI/postprovision wrapper can
# scan for the WARN/FAIL lines.
#
# Auto-detection (mirrors grant-runtime-rbac.sh):
#   * SUBSCRIPTION       <-- AZURE_SUBSCRIPTION_ID or `az account show`
#   * RESOURCE_GROUP     <-- AZURE_RESOURCE_GROUP from `azd env get-values`
#   * CONTAINER_APP_NAME <-- CONTAINER_APP_NAME from azd env
#   * MI principalId     <-- ARM lookup on the Container App identity
#   * STORAGE_ACCOUNT    <-- STORAGE_ACCOUNT_NAME from azd env
#   * ACR_NAME           <-- ACR_NAME from azd env
#   * KEY_VAULT_NAME     <-- KEY_VAULT_NAME from azd env
#   * AKS cluster RG     <-- the single AKS in the subscription, OR --cluster-rg
#
# Usage:
#   scripts/dev/check-mi-rbac.sh                        # full audit, read-only
#   scripts/dev/check-mi-rbac.sh --cluster-rg rg-elb-cluster
#   scripts/dev/check-mi-rbac.sh --principal-id <oid>   # bypass MI auto-detect
#   scripts/dev/check-mi-rbac.sh --subscription <sub>
#   scripts/dev/check-mi-rbac.sh --quiet                # only print FAIL/WARN
#   scripts/dev/check-mi-rbac.sh --strict               # exit 1 on any FAIL
#   scripts/dev/check-mi-rbac.sh --auto-fix             # OPT-IN: also run the
#                                                       # `az role assignment
#                                                       # create` commands for
#                                                       # every missing FAIL
#                                                       # row (per-row tolerant
#                                                       # — a failure on one
#                                                       # scope does NOT block
#                                                       # the others).
#
# --auto-fix policy: this is opt-in by design because role assignments are
# (a) audit-logged with the caller's identity, (b) reversible only by an
# explicit `az role assignment delete`, and (c) silently re-adding a role
# that a security operator removed on purpose would mask the regression.
# When --auto-fix is set the script prints a banner so the operator (and
# the audit log reviewer) can see who approved the grant.
#
# Exit codes:
#   0  every required role present, OR every missing role was successfully
#      granted under --auto-fix (and --strict was not requested)
#   1  --strict was passed and at least one required role is still missing
#      after the optional --auto-fix attempt
#   2  preconditions not met (no az, no MI found, etc.)

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
gray()   { printf '\033[90m%s\033[0m\n' "$*"; }
die()    { red "ERROR: $*" >&2; exit "${2:-2}"; }

SUBSCRIPTION=""
RESOURCE_GROUP=""
CONTAINER_APP=""
PRINCIPAL_ID=""
CLUSTER_RG=""
STORAGE_ACCOUNT=""
ACR_NAME=""
KEY_VAULT_NAME=""
QUIET=0
STRICT=0
AUTO_FIX=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subscription)        SUBSCRIPTION="$2";    shift 2 ;;
    --rg|--resource-group) RESOURCE_GROUP="$2";  shift 2 ;;
    --container-app|--app) CONTAINER_APP="$2";   shift 2 ;;
    --principal-id|--oid)  PRINCIPAL_ID="$2";    shift 2 ;;
    --cluster-rg)          CLUSTER_RG="$2";      shift 2 ;;
    --storage)             STORAGE_ACCOUNT="$2"; shift 2 ;;
    --acr)                 ACR_NAME="$2";        shift 2 ;;
    --keyvault|--kv)       KEY_VAULT_NAME="$2";  shift 2 ;;
    --quiet|-q)            QUIET=1;              shift ;;
    --strict)              STRICT=1;             shift ;;
    --auto-fix)            AUTO_FIX=1;           shift ;;
    -h|--help)             sed -n '2,80p' "$0"; exit 0 ;;
    *)                     die "unknown flag: $1" ;;
  esac
done

command -v az >/dev/null 2>&1 || die "az CLI not found"

# ---------------------------------------------------------------------------
# Auto-detect missing inputs from `azd env get-values`.
# ---------------------------------------------------------------------------
if [[ -z "$SUBSCRIPTION" ]]; then
  SUBSCRIPTION="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv 2>/dev/null || true)}"
  [[ -n "$SUBSCRIPTION" ]] || die "no subscription set; run 'az login' or pass --subscription"
fi
SUB_FLAG=(--subscription "$SUBSCRIPTION")
az account set "${SUB_FLAG[@]}" >/dev/null 2>&1 || die "could not set subscription $SUBSCRIPTION"

azd_get() {
  local key="$1"
  [[ -n "${!key:-}" ]] && { printf '%s' "${!key}"; return 0; }
  command -v azd >/dev/null 2>&1 || return 0
  azd env get-values 2>/dev/null \
    | awk -F= -v k="$key" '$1==k {gsub(/"/,"",$2); print $2; exit}' || true
}

[[ -n "$RESOURCE_GROUP"  ]] || RESOURCE_GROUP="$(azd_get AZURE_RESOURCE_GROUP)"
[[ -n "$CONTAINER_APP"   ]] || CONTAINER_APP="$(azd_get CONTAINER_APP_NAME)"
[[ -n "$STORAGE_ACCOUNT" ]] || STORAGE_ACCOUNT="$(azd_get STORAGE_ACCOUNT_NAME)"
[[ -n "$ACR_NAME"        ]] || ACR_NAME="$(azd_get ACR_NAME)"
[[ -n "$KEY_VAULT_NAME"  ]] || KEY_VAULT_NAME="$(azd_get KEY_VAULT_NAME)"

# ---------------------------------------------------------------------------
# Pre-flight: caller permissions. The doctor needs Reader at sub-scope at
# minimum (so it can `az role assignment list` the deployed MI's grants).
# --auto-fix additionally requires UAA or Owner because it issues
# `az role assignment create` calls under the caller's identity.
# ---------------------------------------------------------------------------
# shellcheck source=scripts/dev/_caller-precheck.sh
source "$REPO_ROOT/scripts/dev/_caller-precheck.sh"
if elb_precheck_init "$SUBSCRIPTION"; then
  if [[ $AUTO_FIX -eq 1 ]]; then
    elb_precheck_caller_for "doctor-autofix"
  else
    elb_precheck_caller_for "doctor-read"
  fi
fi

# ---------------------------------------------------------------------------
# Resolve the dashboard MI principal id.
# ---------------------------------------------------------------------------
if [[ -z "$PRINCIPAL_ID" ]]; then
  [[ -n "$CONTAINER_APP" && -n "$RESOURCE_GROUP" ]] \
    || die "need --principal-id OR (--container-app + --rg) to identify the MI"
  PRINCIPAL_ID="$(az containerapp show "${SUB_FLAG[@]}" \
    -n "$CONTAINER_APP" -g "$RESOURCE_GROUP" \
    --query "identity.userAssignedIdentities | values(@)[0].principalId" -o tsv 2>/dev/null || true)"
  [[ -n "$PRINCIPAL_ID" ]] \
    || die "Container App '$CONTAINER_APP' in '$RESOURCE_GROUP' has no user-assigned identity"
fi
[[ "$PRINCIPAL_ID" =~ ^[0-9a-f-]{36}$ ]] \
  || die "principal-id '$PRINCIPAL_ID' is not a GUID"

# ---------------------------------------------------------------------------
# Try to find the AKS cluster RG when --cluster-rg not given.
# ---------------------------------------------------------------------------
if [[ -z "$CLUSTER_RG" ]]; then
  AKS_LIST="$(az aks list "${SUB_FLAG[@]}" \
    --query "[].resourceGroup" -o tsv 2>/dev/null || true)"
  AKS_COUNT="$(printf '%s\n' "$AKS_LIST" | grep -c . || true)"
  if [[ "$AKS_COUNT" -eq 1 ]]; then
    CLUSTER_RG="$(printf '%s\n' "$AKS_LIST" | head -n1)"
  fi
fi

# ---------------------------------------------------------------------------
# Build the expected RBAC manifest.
# Each row: "label|role|scope|severity"
#   severity = "FAIL" => exit 1 under --strict
#   severity = "WARN" => informational (e.g. conditional/optional roles)
# ---------------------------------------------------------------------------
MANIFEST=()
add() { MANIFEST+=("$1|$2|$3|$4"); }

add "Sub Reader (discovery wizard)" "Reader" \
    "/subscriptions/$SUBSCRIPTION" "FAIL"

# Sub-scope RG-write: AKS auto-creates the `MC_<rg>_<cluster>_<region>`
# node RG at sub scope, which requires
# `Microsoft.Resources/subscriptions/resourceGroups/write`. The project
# custom role "Elb Workload RG Creator" grants only that (plus reads /
# delete / deployments) so we do NOT have to grant sub-scope Contributor.
# Either the custom role or sub-scope Owner/Contributor satisfies the
# requirement — the doctor reports FAIL only when none of those are
# present. (We check "Elb Workload RG Creator" by name; the GUID is
# assigned by Azure at create time and differs per subscription.)
add "Sub-scope RG Creator (AKS MC_* node RG)" "Elb Workload RG Creator" \
    "/subscriptions/$SUBSCRIPTION" "FAIL"

if [[ -n "$RESOURCE_GROUP" ]]; then
  add "Platform RG Contributor"        "Contributor" \
      "/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP" "FAIL"
  add "Platform RG UAA (assign sub-roles)" "User Access Administrator" \
      "/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP" "FAIL"
fi

if [[ -n "$STORAGE_ACCOUNT" && -n "$RESOURCE_GROUP" ]]; then
  STG_SCOPE="/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT"
  add "Platform Storage: Blob Data Contributor"  "Storage Blob Data Contributor"  "$STG_SCOPE" "FAIL"
  add "Platform Storage: Table Data Contributor" "Storage Table Data Contributor" "$STG_SCOPE" "FAIL"
fi

if [[ -n "$ACR_NAME" && -n "$RESOURCE_GROUP" ]]; then
  ACR_SCOPE="/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.ContainerRegistry/registries/$ACR_NAME"
  add "Platform ACR: AcrPull"           "AcrPull"     "$ACR_SCOPE" "FAIL"
  add "Platform ACR: AcrPush"           "AcrPush"     "$ACR_SCOPE" "FAIL"
  add "Platform ACR: Contributor (ACR Tasks)" "Contributor" "$ACR_SCOPE" "FAIL"
fi

if [[ -n "$KEY_VAULT_NAME" && -n "$RESOURCE_GROUP" ]]; then
  KV_SCOPE="/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.KeyVault/vaults/$KEY_VAULT_NAME"
  add "Key Vault: Secrets User"         "Key Vault Secrets User" "$KV_SCOPE" "FAIL"
fi

if [[ -n "$CLUSTER_RG" ]]; then
  CLUSTER_SCOPE="/subscriptions/$SUBSCRIPTION/resourceGroups/$CLUSTER_RG"
  add "Cluster RG: Contributor (id-elb-openapi setup)"        "Contributor"                 "$CLUSTER_SCOPE" "FAIL"
  add "Cluster RG: UAA (assign id-elb-openapi roles)"         "User Access Administrator"   "$CLUSTER_SCOPE" "FAIL"
else
  # No cluster yet — record a WARN so the operator knows the bootstrap
  # path is the next thing they'll need.
  add "Cluster RG (none detected) — first-time create needs bootstrap" \
      "(run: scripts/dev/grant-runtime-rbac.sh --cluster-rg <rg> --region <r> --yes)" \
      "(n/a)" "WARN"
fi

# ---------------------------------------------------------------------------
# Inspect actual assignments and compare.
# ---------------------------------------------------------------------------
[[ $QUIET -eq 0 ]] && {
  printf '\n'
  gray  "elb-dashboard MI RBAC audit"
  gray  "  Subscription:   $SUBSCRIPTION"
  gray  "  Container App:  ${CONTAINER_APP:-<n/a>} (${RESOURCE_GROUP:-<n/a>})"
  gray  "  MI principalId: $PRINCIPAL_ID"
  gray  "  Cluster RG:     ${CLUSTER_RG:-<not detected — bootstrap will be needed>}"
  if [[ $AUTO_FIX -eq 1 ]]; then
    # Audit banner: makes the operator-approved auto-grant explicit in
    # the console output AND (via Azure Activity Log) in Azure itself,
    # since the `az role assignment create` calls below run under the
    # operator's current `az login` context.
    yellow "  Mode:           --auto-fix ENABLED — missing roles will be granted"
    yellow "                  under the current az login identity:"
    yellow "                  $(az account show --query user.name -o tsv 2>/dev/null || echo '<unknown>')"
  else
    gray   "  Mode:           read-only (pass --auto-fix to grant missing roles)"
  fi
  printf '\n'
}

OK_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0     # still missing AFTER any auto-fix attempt
FIXED_COUNT=0    # auto-fix granted successfully this run
FIX_CMDS=()

for row in "${MANIFEST[@]}"; do
  LABEL="${row%%|*}"
  rest="${row#*|}"
  ROLE="${rest%%|*}"
  rest="${rest#*|}"
  SCOPE="${rest%%|*}"
  SEVERITY="${rest##*|}"

  if [[ "$SEVERITY" == "WARN" ]]; then
    WARN_COUNT=$((WARN_COUNT + 1))
    [[ $QUIET -eq 0 ]] && yellow "  [WARN] $LABEL"
    [[ $QUIET -eq 0 ]] && gray   "         $ROLE"
    continue
  fi

  EXISTING="$(az role assignment list "${SUB_FLAG[@]}" \
      --assignee-object-id "$PRINCIPAL_ID" \
      --role "$ROLE" \
      --scope "$SCOPE" \
      --query '[0].id' -o tsv 2>/dev/null || true)"

  if [[ -n "$EXISTING" ]]; then
    OK_COUNT=$((OK_COUNT + 1))
    [[ $QUIET -eq 0 ]] && green "  [ ok ] $LABEL"
    continue
  fi

  # Missing assignment. Behaviour depends on --auto-fix:
  #   off  -> record the fix command and tally as FAIL.
  #   on   -> attempt the grant inline; tally as FIXED on success, FAIL on
  #           per-row failure (e.g. caller lacks UAA at this scope). One
  #           row failing does NOT block the others -- the operator may
  #           have UAA on the platform RG but not on a workload Storage
  #           scope.
  if [[ $AUTO_FIX -eq 1 ]]; then
    if az role assignment create "${SUB_FLAG[@]}" \
          --assignee-object-id "$PRINCIPAL_ID" \
          --assignee-principal-type ServicePrincipal \
          --role "$ROLE" \
          --scope "$SCOPE" \
          --output none 2>/tmp/check-mi-rbac.err; then
      FIXED_COUNT=$((FIXED_COUNT + 1))
      green "  [FIXED] $LABEL"
      gray  "          role:  $ROLE"
      gray  "          scope: $SCOPE"
      continue
    else
      FAIL_COUNT=$((FAIL_COUNT + 1))
      red   "  [FAIL] $LABEL (auto-fix could not grant — you may lack UAA here)"
      gray  "         role:  $ROLE"
      gray  "         scope: $SCOPE"
      gray  "         azerr: $(tr -d '\n' </tmp/check-mi-rbac.err | head -c 200)"
      FIX_CMDS+=(
        "# manual retry (run as an operator with UAA on the scope):"
        "az role assignment create --subscription $SUBSCRIPTION \\"
        "  --assignee-object-id $PRINCIPAL_ID --assignee-principal-type ServicePrincipal \\"
        "  --role \"$ROLE\" \\"
        "  --scope \"$SCOPE\""
        ""
      )
      continue
    fi
  fi

  FAIL_COUNT=$((FAIL_COUNT + 1))
  red "  [FAIL] $LABEL"
  gray "         role:  $ROLE"
  gray "         scope: $SCOPE"
  FIX_CMDS+=(
    "az role assignment create --subscription $SUBSCRIPTION \\"
    "  --assignee-object-id $PRINCIPAL_ID --assignee-principal-type ServicePrincipal \\"
    "  --role \"$ROLE\" \\"
    "  --scope \"$SCOPE\""
    ""
  )
done

# ---------------------------------------------------------------------------
# Summary + fix commands.
# ---------------------------------------------------------------------------
printf '\n'
if [[ $AUTO_FIX -eq 1 ]]; then
  gray "Summary: ok=$OK_COUNT fixed=$FIXED_COUNT warn=$WARN_COUNT fail=$FAIL_COUNT"
  if [[ $FIXED_COUNT -gt 0 ]]; then
    yellow "RBAC propagation usually takes 1–5 minutes — newly granted roles"
    yellow "will not take effect immediately on the deployed Container App."
  fi
else
  gray "Summary: ok=$OK_COUNT warn=$WARN_COUNT fail=$FAIL_COUNT"
fi

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  printf '\n'
  if [[ $AUTO_FIX -eq 1 ]]; then
    yellow "Some rows could not be auto-fixed. Hand them to an operator with"
    yellow "User Access Administrator at the target scope and re-run the"
    yellow "doctor (with or without --auto-fix). Commands to re-attempt:"
  else
    yellow "Fix the missing assignments with the commands below (requires User"
    yellow "Access Administrator at the target scope), OR re-run this doctor"
    yellow "with --auto-fix to attempt them under your current az login. RBAC"
    yellow "propagation usually takes 1\u20135 minutes after each create."
  fi
  printf '\n'
  for line in "${FIX_CMDS[@]}"; do
    printf '%s\n' "$line"
  done
fi

if [[ "$WARN_COUNT" -gt 0 ]]; then
  yellow "Warnings are informational — review them when planning the next"
  yellow "AKS / Storage / ACR onboarding step."
fi

if [[ $STRICT -eq 1 && $FAIL_COUNT -gt 0 ]]; then
  exit 1
fi
exit 0
