#!/usr/bin/env bash
# _caller-precheck.sh — caller-permission preflight library.
#
# Sourced by deploy.sh / cli-upgrade.sh / check-mi-rbac.sh. NOT meant to
# be executed directly. The leading underscore in the filename signals
# this is a helper (mirrors the legacy `_lib*.sh` convention).
#
# Why this exists
# ---------------
# Both the full deployment (azd up + Bicep role assignments) and the
# rolling-update path (`az acr build`, `az role assignment create` under
# --auto-fix-rbac) need specific roles on the calling operator's account.
# Running them with insufficient permissions leaves partial mutations
# behind: a half-built ACR push, an azd state file pointing at a
# never-created Container App, an orphaned MI without sub-scope Reader.
# Catching the missing role *before* any side effect is the only safe
# pattern.
#
# Public functions (callers source the file then call):
#   - elb_precheck_init               populates ELB_CALLER_OID / _UPN / _SUB.
#                                     Returns 1 (does NOT die) when az is
#                                     missing or the caller is not signed
#                                     in, so the caller can show its own
#                                     contextual message.
#   - elb_precheck_caller_for "<mode>"
#                                     verifies the calling user has the
#                                     role set required for `<mode>` and
#                                     dies with an actionable diagnostic
#                                     when not. Modes:
#                                       deploy           Owner OR (Contributor + UAA) at sub
#                                       upgrade-read     any of Owner/Contributor/Reader at sub
#                                       upgrade-write    Owner OR Contributor at sub
#                                       upgrade-autofix  Owner OR UAA at sub (cluster-RG too if --auto-fix-rbac)
#                                       doctor-read      any of Owner/Contributor/Reader at sub
#                                       doctor-autofix   Owner OR UAA at sub
#
# Service-principal callers (`az login --service-principal` or
# `az login --identity` in CI) are tolerated: when `az ad signed-in-user`
# returns nothing the helper falls back to `az ad sp show` against the
# account name. If even that fails, the helper *warns* but does NOT
# block \u2014 the deploy will still fail on the first SDK call with a clear
# Azure error message, and we'd rather not block CI on a mis-detected
# identity.

set -Eeuo pipefail

# Source-guard: protect against double-source.
[[ "${ELB_CALLER_PRECHECK_LOADED:-0}" == "1" ]] && return 0
ELB_CALLER_PRECHECK_LOADED=1

if [[ -z "${_ELB_PRECHECK_RED_DEFINED:-}" ]]; then
  _elb_red()    { printf '\033[31m%s\033[0m\n' "$*" >&2; }
  _elb_yellow() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
  _elb_gray()   { printf '\033[90m%s\033[0m\n' "$*" >&2; }
  _ELB_PRECHECK_RED_DEFINED=1
fi

ELB_CALLER_OID=""
ELB_CALLER_UPN=""
ELB_CALLER_SUB=""
ELB_CALLER_ROLES_AT_SUB=""

# Populate ELB_CALLER_OID / UPN / SUB. Returns 0 on success, 1 on failure
# (caller decides whether to die or skip).
elb_precheck_init() {
  if ! command -v az >/dev/null 2>&1; then
    _elb_red "ERROR: az CLI not found; install Azure CLI before running this script."
    return 1
  fi

  ELB_CALLER_SUB="${1:-${AZURE_SUBSCRIPTION_ID:-}}"
  if [[ -z "$ELB_CALLER_SUB" ]]; then
    ELB_CALLER_SUB="$(az account show --query id -o tsv 2>/dev/null || true)"
  fi
  if [[ -z "$ELB_CALLER_SUB" ]]; then
    _elb_red "ERROR: no subscription set. Run 'az login' first or pass --subscription."
    return 1
  fi

  ELB_CALLER_UPN="$(az account show --query user.name -o tsv 2>/dev/null || echo '<unknown>')"

  # Resolve the caller's Entra object id. Users return via signed-in-user;
  # SPs/MIs need a sp show lookup against the account name.
  ELB_CALLER_OID="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
  if [[ -z "$ELB_CALLER_OID" ]]; then
    # SP/MI: account.user.name is the SP appId or MI client id.
    local sp_name
    sp_name="$(az account show --query user.name -o tsv 2>/dev/null || true)"
    if [[ -n "$sp_name" ]]; then
      ELB_CALLER_OID="$(az ad sp show --id "$sp_name" --query id -o tsv 2>/dev/null || true)"
    fi
  fi
  if [[ -z "$ELB_CALLER_OID" ]]; then
    _elb_yellow "WARN: could not resolve caller object id for '$ELB_CALLER_UPN'."
    _elb_yellow "      Permission preflight will be skipped; the script may still"
    _elb_yellow "      fail later if the caller lacks the required Azure roles."
    return 1
  fi
  return 0
}

# Internal: list the caller's role assignments at sub scope (incl. inherited)
# and cache in ELB_CALLER_ROLES_AT_SUB (newline-separated, unique-sorted).
_elb_load_roles_at_sub() {
  [[ -n "$ELB_CALLER_ROLES_AT_SUB" ]] && return 0
  ELB_CALLER_ROLES_AT_SUB="$(
    az role assignment list \
      --assignee-object-id "$ELB_CALLER_OID" \
      --scope "/subscriptions/$ELB_CALLER_SUB" \
      --include-inherited \
      --query '[].roleDefinitionName' -o tsv 2>/dev/null \
      | sort -u || true
  )"
}

_elb_has_role() {
  _elb_load_roles_at_sub
  printf '%s\n' "$ELB_CALLER_ROLES_AT_SUB" | grep -qFx "$1"
}

# Print the role assignments the caller actually has so the operator can
# see why preflight failed.
_elb_print_actual_roles() {
  _elb_load_roles_at_sub
  if [[ -n "$ELB_CALLER_ROLES_AT_SUB" ]]; then
    _elb_red "Your current role assignments at /subscriptions/$ELB_CALLER_SUB"
    _elb_red "(including inherited from management groups):"
    while IFS= read -r r; do
      [[ -n "$r" ]] && _elb_red "   - $r"
    done <<<"$ELB_CALLER_ROLES_AT_SUB"
  else
    _elb_red "You currently have NO role assignments visible at this scope."
    _elb_red "Either you have no access at all, or you lack 'Microsoft.Authorization/"
    _elb_red "roleAssignments/read' which is needed even to list your own grants."
  fi
}

# Die with a remediation hint and exit non-zero.
_elb_precheck_die() {
  local mode="$1"
  shift
  _elb_red ""
  _elb_red "\u274c Insufficient permissions to run this script as '$ELB_CALLER_UPN'."
  _elb_red ""
  _elb_red "Mode '$mode' requires one of:"
  for line in "$@"; do
    _elb_red "  - $line"
  done
  _elb_red ""
  _elb_print_actual_roles
  _elb_red ""
  _elb_red "Ask a subscription Owner to grant you the missing role(s):"
  _elb_red ""
  _elb_red "  az role assignment create --subscription $ELB_CALLER_SUB \\"
  _elb_red "    --assignee-object-id $ELB_CALLER_OID \\"
  _elb_red "    --assignee-principal-type User \\"
  _elb_red "    --role <role-name> \\"
  _elb_red "    --scope /subscriptions/$ELB_CALLER_SUB"
  _elb_red ""
  _elb_red "RBAC propagation usually takes 1\u20135 minutes after the grant lands."
  exit 4
}

# Public: enforce the role set required for a given mode.
elb_precheck_caller_for() {
  local mode="${1:-}"
  [[ -n "$mode" ]] || { _elb_red "elb_precheck_caller_for: mode required"; return 2; }

  # If init failed (caller called us anyway), behave permissively \u2014 the
  # script's own error path will surface the underlying issue.
  [[ -n "$ELB_CALLER_OID" ]] || return 0

  case "$mode" in
    deploy)
      # azd up needs to create new RGs (sub-scope write) AND assign roles
      # inside Bicep modules (UAA at sub or RG). Owner covers both.
      if _elb_has_role "Owner"; then
        return 0
      fi
      if _elb_has_role "Contributor" && _elb_has_role "User Access Administrator"; then
        return 0
      fi
      _elb_precheck_die "$mode" \
        "Owner at /subscriptions/$ELB_CALLER_SUB" \
        "OR ('Contributor' AND 'User Access Administrator') at /subscriptions/$ELB_CALLER_SUB"
      ;;
    upgrade-write)
      # cli-upgrade.sh needs ACR build/push + Container App patch on the
      # platform RG. Sub-Contributor is the cleanest signal; we allow
      # Owner as a superset. If the caller has neither at sub, they
      # might still have Contributor at RG scope only \u2014 that case is
      # rare for cli-upgrade operators and we ask them to re-run with
      # `az account set` against the right context, OR the script will
      # fail loudly on the first `az containerapp update` with a clear
      # Azure error.
      if _elb_has_role "Owner" || _elb_has_role "Contributor"; then
        return 0
      fi
      _elb_precheck_die "$mode" \
        "'Owner' OR 'Contributor' at /subscriptions/$ELB_CALLER_SUB" \
        "(needed for 'az acr build' and 'az containerapp update' on the platform RG)"
      ;;
    upgrade-read|doctor-read)
      # Read-only doctor / cli-upgrade --dry-run path. Needs Reader at
      # minimum to list role assignments.
      if _elb_has_role "Owner" || _elb_has_role "Contributor" || _elb_has_role "Reader"; then
        return 0
      fi
      _elb_precheck_die "$mode" \
        "At least 'Reader' at /subscriptions/$ELB_CALLER_SUB" \
        "(needed to enumerate the deployed MI's existing role assignments)"
      ;;
    upgrade-autofix|doctor-autofix)
      # Writes role assignments under the operator's identity.
      if _elb_has_role "Owner" || _elb_has_role "User Access Administrator"; then
        return 0
      fi
      _elb_precheck_die "$mode" \
        "'Owner' OR 'User Access Administrator' at /subscriptions/$ELB_CALLER_SUB" \
        "(needed because --auto-fix grants role assignments under your identity;" \
        " omit the auto-fix flag to stay in read-only doctor mode)"
      ;;
    *)
      _elb_red "elb_precheck_caller_for: unknown mode '$mode'"
      return 2
      ;;
  esac
}
