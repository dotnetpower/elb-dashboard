#!/usr/bin/env bash
# One-command bootstrap for a fresh clone.

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: ./deploy.sh [--prepare-only]

Checks Azure CLI login, prepares the default azd environment, runs azd up,
and opens the deployed Container App URL.

Environment overrides:
  AZD_ENV_NAME                 Default: elb-dashboard
  AZURE_LOCATION               Default: koreacentral
  LOCKDOWN_PRIVATE_NETWORKING  Default: false
  ALLOWED_ORIGINS              Default: empty / same-origin
  ENABLE_APPLICATION_INSIGHTS  Default: false
  ELB_EXISTING_RG_ACTION       delete | number | abort when rg-elb-dashboard has resources
  ELB_RESOURCE_NAME_SUFFIX     Optional suffix such as -01 for numbered deployments
  ELB_RESOURCE_NAME_SLOT       Optional azd-safe slot such as slot01 for numbered deployments
  ELB_ALLOW_AZD_ENV_RETARGET   true to allow overwriting an existing azd env target subscription/tenant
                               without prompting. In an interactive TTY the script
                               otherwise asks whether to (1) switch Azure CLI to
                               match the azd env target, (2) retarget the azd env,
                               or (3) abort. In a non-interactive shell the script
                               errors instead of prompting.
  ELB_SKIP_LOCAL_RBAC          true to skip granting local-debug RBAC to the deployer
  ELB_AUTO_FIX_RBAC            true (default) to let the MI RBAC doctor auto-grant
                               any missing resource-to-resource role assignment
                               (Sub Reader, Platform RG Contributor+UAA, Storage
                               Blob/Table Data Contributor, ACR AcrPull/AcrPush/
                               Contributor, Key Vault Secrets User, optional
                               cluster RG Contributor+UAA) under the current az
                               login identity. Set false for security-audited
                               environments that require an Owner / UAA to apply
                               the role assignments out of band; the doctor will
                               then only report the gaps and print the exact
                               `az role assignment create` commands.
  ELB_BOOTSTRAP_CLUSTER_RG     true (default) to auto-bootstrap the AKS cluster RG
                               (create the RG + grant the dashboard MI Contributor
                               + UAA on that RG only) so the SPA's first "Create
                               Cluster" click succeeds without granting the MI
                               Contributor at subscription scope. Set false to
                               keep the legacy least-privilege posture (the
                               operator must run grant-runtime-rbac.sh by hand
                               before the first cluster create).
  ELB_CLUSTER_RG_NAME          Override the default cluster RG name (default:
                               rg-elb-cluster) for the bootstrap above.
  ELB_CLUSTER_RG_REGION        Override the region (default: $AZURE_LOCATION) for the
                               bootstrap above.

USAGE
}

prepare_only=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prepare-only) prepare_only=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

env_name="${AZD_ENV_NAME:-elb-dashboard}"
location="${AZURE_LOCATION:-koreacentral}"
lockdown="${LOCKDOWN_PRIVATE_NETWORKING:-false}"
allowed_origins="${ALLOWED_ORIGINS:-}"
enable_application_insights="${ENABLE_APPLICATION_INSIGHTS:-false}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: $1 is required." >&2
    exit 1
  fi
}

open_url() {
  local url="$1"
  if [[ -z "$url" ]]; then
    return 0
  fi
  echo "==> Opening $url"
  if command -v xdg-open >/dev/null 2>&1; then
    (xdg-open "$url" >/dev/null 2>&1 &)
  elif command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  elif command -v wslview >/dev/null 2>&1; then
    wslview "$url" >/dev/null 2>&1 || true
  else
    echo "==> Browser opener not found. Open this URL manually: $url"
  fi
}

azd_env_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key {gsub(/"/, "", $2); print $2; exit}'
}

is_true() {
  case "${1:-}" in
    true|TRUE|1|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

need_cmd az
need_cmd azd

bash "$repo_root/scripts/dev/azd-progress.sh" plan
export ELB_AZD_PROGRESS_PLAN_SHOWN=true
echo "==> Full deployment usually takes 10-20 minutes. Code-only changes are faster with scripts/dev/quick-deploy.sh."
bash "$repo_root/scripts/dev/azd-progress.sh" step 0 "Local bootstrap" "Checking Azure CLI, azd auth, and azd environment values."

if ! az account show -o none >/dev/null 2>&1; then
  echo "==> Azure CLI is not signed in; starting az login..."
  az login >/dev/null
fi

subscription_id="$(az account show --query id -o tsv)"
tenant_id="$(az account show --query tenantId -o tsv)"
account_name="$(az account show --query name -o tsv)"
user_name="$(az account show --query user.name -o tsv)"

echo "==> Azure CLI account"
echo "    User:         $user_name"
echo "    Subscription: $account_name ($subscription_id)"
echo "    Tenant:       $tenant_id"

existing_env_values="$(azd env get-values --environment "$env_name" 2>/dev/null || true)"
existing_subscription_id="$(printf '%s\n' "$existing_env_values" | azd_env_value AZURE_SUBSCRIPTION_ID)"
existing_tenant_id="$(printf '%s\n' "$existing_env_values" | azd_env_value AZURE_TENANT_ID)"

subscription_mismatch=false
tenant_mismatch=false
if [[ -n "$existing_subscription_id" && "$existing_subscription_id" != "$subscription_id" ]]; then
  subscription_mismatch=true
fi
if [[ -n "$existing_tenant_id" && "$existing_tenant_id" != "$tenant_id" ]]; then
  tenant_mismatch=true
fi

if ! is_true "${ELB_ALLOW_AZD_ENV_RETARGET:-}" && { [[ "$subscription_mismatch" == "true" ]] || [[ "$tenant_mismatch" == "true" ]]; }; then
  target_tenant_id="${existing_tenant_id:-$tenant_id}"

  cat >&2 <<EOF
==> Azure CLI / azd environment mismatch detected
    azd environment '$env_name' (from .azure/$env_name/.env):
      Subscription: $existing_subscription_id
      Tenant:       ${existing_tenant_id:-<unset>}
    Active Azure CLI context:
      Subscription: $subscription_id ($account_name)
      Tenant:       $tenant_id

EOF

  if [[ ! -t 0 ]] || [[ ! -t 1 ]]; then
    cat >&2 <<EOF
ERROR: refusing to silently retarget the azd environment in a non-interactive shell.

Choose one of these paths, then rerun ./deploy.sh:

1. Use the active Azure CLI context (retarget the azd environment):
  ELB_ALLOW_AZD_ENV_RETARGET=true ./deploy.sh

2. Keep the existing azd environment target:
  az account set --subscription "$existing_subscription_id"$( [[ "$tenant_mismatch" == "true" ]] && printf '\n  az login --tenant "%s"\n  azd auth login --use-device-code --tenant-id "%s"' "$target_tenant_id" "$target_tenant_id" )
EOF
    exit 1
  fi

  cat >&2 <<EOF
How would you like to proceed?
  1) Use the active Azure CLI context — retarget the azd environment to it (sets ELB_ALLOW_AZD_ENV_RETARGET=true)
  2) Keep the azd environment target — switch Azure CLI to match it (runs az account set$( [[ "$tenant_mismatch" == "true" ]] && printf ' + az login --tenant' ))
  3) Abort and let me fix it manually
EOF

  mismatch_choice=""
  while true; do
    printf 'Enter choice [1/2/3] (default 3): ' >&2
    if ! read -r mismatch_choice; then
      mismatch_choice="3"
    fi
    mismatch_choice="${mismatch_choice:-3}"
    case "$mismatch_choice" in
      1|2|3) break ;;
      *) echo "Invalid choice: $mismatch_choice" >&2 ;;
    esac
  done

  case "$mismatch_choice" in
    1)
      echo "==> Retargeting azd environment '$env_name' to the active Azure CLI context" >&2
      export ELB_ALLOW_AZD_ENV_RETARGET=true
      ;;
    2)
      if [[ "$tenant_mismatch" == "true" ]]; then
        echo "==> Switching Azure CLI to tenant $target_tenant_id (browser login required)" >&2
        az login --tenant "$target_tenant_id" >/dev/null
      fi
      echo "==> Setting Azure CLI subscription to $existing_subscription_id" >&2
      az account set --subscription "$existing_subscription_id"
      subscription_id="$(az account show --query id -o tsv)"
      tenant_id="$(az account show --query tenantId -o tsv)"
      account_name="$(az account show --query name -o tsv)"
      user_name="$(az account show --query user.name -o tsv)"
      echo "    User:         $user_name"
      echo "    Subscription: $account_name ($subscription_id)"
      echo "    Tenant:       $tenant_id"
      if [[ "$subscription_id" != "$existing_subscription_id" ]]; then
        echo "ERROR: 'az account set' did not switch to '$existing_subscription_id'. Run 'az login' against that tenant first." >&2
        exit 1
      fi
      ;;
    3)
      cat >&2 <<EOF
Aborted. To fix manually, run one of:

1. Use the active Azure CLI context (retarget the azd environment):
  ELB_ALLOW_AZD_ENV_RETARGET=true ./deploy.sh

2. Keep the existing azd environment target:
  az account set --subscription "$existing_subscription_id"$( [[ "$tenant_mismatch" == "true" ]] && printf '\n  az login --tenant "%s"\n  azd auth login --use-device-code --tenant-id "%s"' "$target_tenant_id" "$target_tenant_id" )
EOF
      exit 1
      ;;
  esac
fi

azd_status="$(azd auth login --check-status 2>&1 || true)"
if [[ "$azd_status" != *"$user_name"* ]]; then
  echo "==> azd is not signed in as the active Azure CLI user; starting device-code login..."
  echo "    Use the same browser account as Azure CLI: $user_name"
  azd auth login --use-device-code --tenant-id "$tenant_id"
  azd_status="$(azd auth login --check-status 2>&1 || true)"
  if [[ "$azd_status" != *"$user_name"* ]]; then
    cat >&2 <<EOF
ERROR: azd is still not signed in as the active Azure CLI user '$user_name'.

The browser device-code flow likely completed with a different account. Sign out
or switch accounts so Azure CLI and azd use the same tenant/user, then rerun.
EOF
    exit 1
  fi
fi

if azd env get-values --environment "$env_name" >/dev/null 2>&1; then
  echo "==> Selecting existing azd environment: $env_name"
  azd env select "$env_name" --no-prompt >/dev/null
else
  echo "==> Creating azd environment: $env_name"
  azd env new "$env_name" \
    --location "$location" \
    --subscription "$subscription_id" \
    --no-prompt >/dev/null
fi

echo "==> Configuring azd environment"
azd env set --environment "$env_name" AZURE_LOCATION "$location" >/dev/null
azd env set --environment "$env_name" AZURE_SUBSCRIPTION_ID "$subscription_id" >/dev/null
azd env set --environment "$env_name" AZURE_TENANT_ID "$tenant_id" >/dev/null
azd env set --environment "$env_name" ALLOWED_ORIGINS "$allowed_origins" >/dev/null
azd env set --environment "$env_name" LOCKDOWN_PRIVATE_NETWORKING "$lockdown" >/dev/null
azd env set --environment "$env_name" ENABLE_APPLICATION_INSIGHTS "$enable_application_insights" >/dev/null

existing_resource_slot="$(azd env get-values --environment "$env_name" 2>/dev/null | awk -F= '/^ELB_RESOURCE_NAME_SLOT=/{gsub(/"/, "", $2); print $2; exit}')"
if [[ -v ELB_RESOURCE_NAME_SUFFIX ]]; then
  export ELB_RESOURCE_NAME_SUFFIX
elif [[ -v ELB_RESOURCE_NAME_SLOT ]]; then
  export ELB_RESOURCE_NAME_SLOT
else
  if [[ -n "$existing_resource_slot" ]]; then
    base_rg_exists="$(az group exists --name rg-elb-dashboard -o tsv 2>/dev/null || echo false)"
    base_rg_count="0"
    if [[ "$base_rg_exists" == "true" ]]; then
      base_rg_count="$(az resource list --resource-group rg-elb-dashboard --query 'length(@)' -o tsv 2>/dev/null || echo 0)"
    fi
    if [[ "$base_rg_exists" == "true" && "$base_rg_count" != "0" ]]; then
      export ELB_RESOURCE_NAME_SLOT="$existing_resource_slot"
    else
      echo "==> Clearing stale numbered resource slot: $existing_resource_slot (rg-elb-dashboard is available)"
      export ELB_RESOURCE_NAME_SLOT=""
    fi
  else
    export ELB_RESOURCE_NAME_SLOT=""
  fi
fi

echo "==> Pre-flight provider check"
bash "$repo_root/scripts/dev/register-providers.sh" --subscription "$subscription_id"
export ELB_PROVIDER_REGISTRATION_READY=true

# ---------------------------------------------------------------------------
# Pre-flight: caller permissions. Verify the operator running deploy.sh has
# the role set Bicep will need (Owner OR Contributor+UAA at subscription
# scope) BEFORE we hand control over to `azd up`. Without this we discover
# the gap 10+ minutes in, leaving a half-created RG and an azd state file
# pointing at a Container App that never came up. The helper exits non-zero
# with a clear remediation hint if the caller is under-privileged; if it
# cannot determine the caller it warns and proceeds (CI/SP edge cases).
# ---------------------------------------------------------------------------
# shellcheck source=scripts/dev/_caller-precheck.sh
source "$repo_root/scripts/dev/_caller-precheck.sh"
if elb_precheck_init "$subscription_id"; then
  echo "==> Pre-flight: verifying caller permissions for full deployment"
  elb_precheck_caller_for "deploy"
  echo "    \u2713 caller '$ELB_CALLER_UPN' has the roles required for azd up"
fi

bash "$repo_root/scripts/dev/resolve-resource-group.sh" --subscription "$subscription_id" --environment "$env_name"
resource_slot="$(azd env get-values --environment "$env_name" 2>/dev/null | awk -F= '/^ELB_RESOURCE_NAME_SLOT=/{gsub(/"/, "", $2); print $2; exit}')"
resource_suffix="${resource_slot#slot}"
if [[ -n "$resource_slot" ]]; then
  resource_suffix="-${resource_suffix}"
fi
echo "==> Target resource group: rg-elb-dashboard${resource_suffix}"
if [[ "$prepare_only" == "true" ]]; then
  bash "$repo_root/scripts/dev/azd-progress.sh" "done" 0 "Local bootstrap"
  echo "==> Prepare-only mode complete. Run azd up to deploy."
  exit 0
fi

bash "$repo_root/scripts/dev/azd-progress.sh" "done" 0 "Local bootstrap"
echo "==> Running azd up"
azd up \
  --environment "$env_name" \
  --location "$location" \
  --subscription "$subscription_id" \
  --no-prompt

post_deploy_env_values="$(azd env get-values --environment "$env_name" 2>/dev/null || true)"
deployed_rg="$(printf '%s\n' "$post_deploy_env_values" | azd_env_value AZURE_RESOURCE_GROUP)"
deployed_storage="$(printf '%s\n' "$post_deploy_env_values" | azd_env_value STORAGE_ACCOUNT_NAME)"
deployed_acr="$(printf '%s\n' "$post_deploy_env_values" | azd_env_value ACR_NAME)"

if is_true "${ELB_SKIP_LOCAL_RBAC:-false}"; then
  echo "==> Skipping local-debug RBAC grant (ELB_SKIP_LOCAL_RBAC=true)"
elif [[ -n "$deployed_rg" && -n "$deployed_storage" ]]; then
  echo "==> Granting local-debug RBAC to the deployer"
  grant_args=(
    --subscription "$subscription_id"
    --storage "$deployed_storage"
    --storage-rg "$deployed_rg"
  )
  if [[ -n "$deployed_acr" ]]; then
    grant_args+=(--acr "$deployed_acr" --acr-rg "$deployed_rg")
  fi
  if bash "$repo_root/scripts/dev/grant-local-rbac.sh" "${grant_args[@]}"; then
    echo "==> Local-debug RBAC ready for $user_name"
  else
    cat >&2 <<EOF
WARN: Could not grant local-debug RBAC to '$user_name'. Deployment is complete,
but local host-mode API reads may show Storage access_denied until an Owner or
User Access Administrator runs:

  scripts/dev/grant-local-rbac.sh --subscription "$subscription_id" --storage "$deployed_storage" --storage-rg "$deployed_rg"

EOF
  fi
else
  echo "==> Skipping local-debug RBAC grant (azd did not expose Storage outputs)"
fi

app_url="$(printf '%s\n' "$post_deploy_env_values" | azd_env_value CONTAINER_APP_URL)"
if [[ -z "$app_url" ]]; then
  app_fqdn="$(printf '%s\n' "$post_deploy_env_values" | azd_env_value CONTAINER_APP_FQDN)"
  if [[ -n "$app_fqdn" ]]; then
    app_url="https://$app_fqdn"
  fi
fi

# ---------------------------------------------------------------------------
# Permission doctor (auto-fix by default).
#
# Bicep is idempotent for the role assignments it owns, but it cannot detect
# (a) orphaned assignments from a previous MI principalId, (b) MI roles
# missing on pre-existing workload Storage/ACR that the SPA wizard attaches
# to, or (c) the cluster-RG bootstrap gap. `check-mi-rbac.sh` enumerates the
# expected {scope, role} pairs and grants any missing ones under the current
# az login identity. Set ELB_AUTO_FIX_RBAC=false to fall back to read-only
# mode (security-audited environments that require an Owner / UAA to apply
# the role assignments out of band).
# ---------------------------------------------------------------------------
DOCTOR_SCRIPT="$repo_root/scripts/dev/check-mi-rbac.sh"
DOCTOR_OUTPUT=""
if [[ -x "$DOCTOR_SCRIPT" ]]; then
  doctor_args=(--subscription "$subscription_id")
  if is_true "${ELB_AUTO_FIX_RBAC:-true}"; then
    echo "==> Running MI RBAC doctor (--auto-fix: missing resource-to-resource roles"
    echo "    will be granted under '$user_name'; opt-out with ELB_AUTO_FIX_RBAC=false)"
    doctor_args+=(--auto-fix)
  else
    echo "==> Running MI RBAC doctor (read-only, ELB_AUTO_FIX_RBAC=false)"
    echo "    Set ELB_AUTO_FIX_RBAC=true (default) to also grant any missing roles in-line."
  fi
  # Tee the doctor output to both the console and a variable so we can
  # detect the "no cluster yet" branch and offer the bootstrap below.
  if ! DOCTOR_OUTPUT="$(bash "$DOCTOR_SCRIPT" "${doctor_args[@]}" 2>&1 | tee /dev/tty)"; then
    echo "==> MI RBAC doctor reported unresolved gaps — see the fix commands above."
  fi
fi

# ---------------------------------------------------------------------------
# Cluster-RG bootstrap (closes the bc0fcf1 first-time-cluster-create gap).
#
# The dashboard MI is granted only Reader at subscription scope, so the
# very first SPA "Create Cluster" click fails when the cluster RG does
# not exist yet (resourceGroups/write at sub scope is denied). We close
# that gap by pre-creating the RG and granting Contributor + UAA on it
# only — same pair `infra/modules/workloadClusterRoles.bicep` would apply
# on the second `azd provision`, but available immediately after the
# first `azd up`.
#
# Trigger conditions:
#   * ELB_BOOTSTRAP_CLUSTER_RG=true (default) AND doctor printed
#     "no AKS cluster found" — auto-create + grant. Interactive shells
#     get a Y/n confirmation with default Y; non-interactive (CI) runs
#     proceed straight to the bootstrap so a fresh `./deploy.sh` always
#     produces a workspace where "Create Cluster" can run.
#   * ELB_BOOTSTRAP_CLUSTER_RG=false — skipped; deploy.sh only prints the
#     manual command in the closing summary. Use this when policy forbids
#     `deploy.sh` from creating workload resource groups.
#
# The "no cluster yet" condition is detected by a real `az aks list` against
# the subscription, not by grepping the doctor's stdout. The doctor's WARN
# row about a missing cluster RG uses different phrasing across versions, and
# relying on a specific substring made this entire bootstrap block dead code
# in the past.
# ---------------------------------------------------------------------------
CLUSTER_BOOTSTRAP_RAN=false
CLUSTER_BOOTSTRAP_HINT=false
existing_aks_id="$(az aks list --subscription "$subscription_id" --query '[0].id' -o tsv 2>/dev/null || true)"
if [[ -z "$existing_aks_id" ]]; then
  BOOTSTRAP_RG="${ELB_CLUSTER_RG_NAME:-rg-elb-cluster}"
  BOOTSTRAP_REGION="${ELB_CLUSTER_RG_REGION:-$location}"
  RBAC_SCRIPT="$repo_root/scripts/dev/grant-runtime-rbac.sh"

  if [[ ! -x "$RBAC_SCRIPT" ]]; then
    echo "==> Cluster-RG bootstrap skipped — helper script missing: $RBAC_SCRIPT"
  elif ! is_true "${ELB_BOOTSTRAP_CLUSTER_RG:-true}"; then
    echo "==> Cluster-RG bootstrap skipped (ELB_BOOTSTRAP_CLUSTER_RG=false)."
    CLUSTER_BOOTSTRAP_HINT=true
  elif [[ -t 0 && -t 1 ]]; then
    echo ""
    echo "==> The dashboard MI does not yet have access to a cluster RG."
    echo "    The SPA's first \"Create Cluster\" click will fail with"
    echo "    AuthorizationFailed (resourceGroups/write at sub scope) unless"
    echo "    the cluster RG is pre-created and the MI is granted Contributor."
    echo ""
    read -r -p "Pre-create the cluster RG + grant MI roles now? [Y/n] " _ans
    case "${_ans:-Y}" in
      n|N|no|No|NO)
        echo "    Skipping bootstrap. Run later with:"
        echo "      bash scripts/dev/grant-runtime-rbac.sh \\"
        echo "        --cluster-rg $BOOTSTRAP_RG --region $BOOTSTRAP_REGION --yes"
        CLUSTER_BOOTSTRAP_HINT=true
        ;;
      *)
        read -r -p "  Cluster RG name [$BOOTSTRAP_RG]: " _rg
        BOOTSTRAP_RG="${_rg:-$BOOTSTRAP_RG}"
        read -r -p "  Region [$BOOTSTRAP_REGION]: " _region
        BOOTSTRAP_REGION="${_region:-$BOOTSTRAP_REGION}"
        echo "==> Running grant-runtime-rbac.sh --cluster-rg $BOOTSTRAP_RG --region $BOOTSTRAP_REGION"
        if bash "$RBAC_SCRIPT" --cluster-rg "$BOOTSTRAP_RG" --region "$BOOTSTRAP_REGION" --yes; then
          CLUSTER_BOOTSTRAP_RAN=true
        else
          echo "==> Bootstrap failed — see the error above. Manual command:"
          echo "      bash scripts/dev/grant-runtime-rbac.sh \\"
          echo "        --cluster-rg $BOOTSTRAP_RG --region $BOOTSTRAP_REGION --yes"
        fi
        ;;
    esac
  else
    # Non-interactive (CI). Default is bootstrap-on so the dashboard's
    # first "Create Cluster" works without follow-up steps.
    echo "==> Cluster-RG bootstrap (non-interactive default; opt-out with ELB_BOOTSTRAP_CLUSTER_RG=false)"
    echo "    Creating '$BOOTSTRAP_RG' in '$BOOTSTRAP_REGION' and granting MI roles."
    if bash "$RBAC_SCRIPT" --cluster-rg "$BOOTSTRAP_RG" --region "$BOOTSTRAP_REGION" --yes; then
      CLUSTER_BOOTSTRAP_RAN=true
    else
      echo "==> Bootstrap failed — see the error above. Manual command:"
      echo "      bash scripts/dev/grant-runtime-rbac.sh \\"
      echo "        --cluster-rg $BOOTSTRAP_RG --region $BOOTSTRAP_REGION --yes"
      CLUSTER_BOOTSTRAP_HINT=true
    fi
  fi
fi

if [[ -n "$app_url" ]]; then
  echo "==> Deployment complete: $app_url"
  if $CLUSTER_BOOTSTRAP_RAN; then
    echo "    Cluster RG '$BOOTSTRAP_RG' is ready. Wait 1–5 min for RBAC propagation,"
    echo "    then click 'Create Cluster' in the dashboard."
  elif $CLUSTER_BOOTSTRAP_HINT; then
    echo ""
    echo "    [!] Before clicking 'Create Cluster' in the dashboard, run:"
    echo "        bash scripts/dev/grant-runtime-rbac.sh \\"
    echo "          --cluster-rg ${ELB_CLUSTER_RG_NAME:-rg-elb-cluster} \\"
    echo "          --region ${ELB_CLUSTER_RG_REGION:-$location} --yes"
  fi
  open_url "$app_url"
else
  echo "==> Deployment complete. Run 'azd env get-values --environment $env_name' to inspect outputs."
fi