#!/usr/bin/env bash
# Shared Azure CLI context guards for manual deploy tools.
#
# Single entry point: `prepare_deploy_env_from_az_login` — call once at the
# top of a manual deploy script (`quick-deploy.sh`, `cli-upgrade.sh`).
# It guarantees that after the call returns:
#
#   * `az account show` is the source of truth: AZURE_SUBSCRIPTION_ID and
#     AZURE_TENANT_ID are exported to match the active `az login`.
#   * AZURE_RESOURCE_GROUP, ACR_NAME, ACR_LOGIN_SERVER, CONTAINER_APP_NAME,
#     CONTAINER_APP_FQDN, CONTAINER_ENV_NAME, STORAGE_ACCOUNT_NAME,
#     KEY_VAULT_NAME, LOG_ANALYTICS_WORKSPACE_ID,
#     APPLICATIONINSIGHTS_CONNECTION_STRING, SHARED_IDENTITY_RESOURCE_ID,
#     SHARED_IDENTITY_CLIENT_ID, SHARED_IDENTITY_PRINCIPAL_ID,
#     API_CLIENT_ID, VITE_AZURE_CLIENT_ID — all populated from ARM lookups
#     against THAT subscription, not from whatever `/tmp/azd-env.sh` was
#     last sourced from a different env.
#   * The currently selected azd env (when any) is updated with the same
#     values (`azd env set ...`) so the next shell also starts aligned.
#
# Why this exists:
#   * Operators frequently have several Azure subscriptions (e.g. personal
#     MCAP + a colleague's MCAP) all logged into az CLI at once. The
#     deployed prod environment lives in only ONE of them, but azd env
#     state on disk can easily point at a sister sub from a prior `azd up`.
#   * The old behavior of `az account set --subscription
#     $AZURE_SUBSCRIPTION_ID` (silent az switch) put the operator on the
#     azd env sub regardless of what `az account show` reported, which is
#     how "pushed image to the wrong tenant" incidents happened.
#   * Aborting on mismatch is correct but pushes a brittle 7-step manual
#     recovery onto every fresh shell. Operators almost always want
#     "deploy against the sub I am logged into right now".
#
# Override: if you EXPLICITLY want a deploy to use a sub different from
# the active `az login`, do this BEFORE calling the deploy script:
#
#   az account set --subscription <target-sub>
#
# Then the helper sees `az account show` == target sub and discovers
# resources in that one.
#
# Note: this is the contract for MANUAL deploy tools that the operator
# runs from a shell with their own `az login` (quick-deploy.sh,
# cli-upgrade.sh). postprovision.sh runs as an azd hook where azd env IS
# authoritative; it intentionally keeps its own silent `az account set`
# (see scripts/dev/postprovision.sh line ~100).

# -----------------------------------------------------------------------
# Internal: subscription-scoped env vars that the deploy scripts consume.
# When the active subscription changes, these MUST be re-derived against
# the new sub before anything else looks at them.
# -----------------------------------------------------------------------
_AZ_CONTEXT_SUBSCRIPTION_SCOPED_VARS=(
  AZURE_RESOURCE_GROUP
  AZURE_LOCATION
  ACR_NAME
  ACR_LOGIN_SERVER
  CONTAINER_APP_NAME
  CONTAINER_APP_FQDN
  CONTAINER_ENV_NAME
  STORAGE_ACCOUNT_NAME
  KEY_VAULT_NAME
  LOG_ANALYTICS_WORKSPACE_ID
  APPLICATIONINSIGHTS_CONNECTION_STRING
  SHARED_IDENTITY_RESOURCE_ID
  SHARED_IDENTITY_CLIENT_ID
  SHARED_IDENTITY_PRINCIPAL_ID
  API_CLIENT_ID
  VITE_AZURE_CLIENT_ID
)

_az_context_log() { printf '[az-context] %s\n' "$*" >&2; }
_az_context_warn() { printf '[az-context] ⚠ %s\n' "$*" >&2; }

_az_context_current_azd_env_name() {
  command -v azd >/dev/null 2>&1 || { printf ''; return; }
  # Avoid piping into `head` here. Under callers with `set -Eeuo pipefail`
  # the SIGPIPE that bash sends to `azd env get-name` when `head` closes
  # the pipe after the first line propagates as exit 141 through the
  # pipeline and trips `set -e`, killing the deploy script silently right
  # after the opening banner.
  local raw name
  raw="$(azd env get-name 2>/dev/null || true)"
  name="${raw%%$'\n'*}"
  # When no azd env is selected, `azd env get-name` writes multi-line
  # help text to stdout instead of failing. A real env name is a single
  # token with no internal whitespace.
  name="${name#"${name%%[![:space:]]*}"}"
  name="${name%"${name##*[![:space:]]}"}"
  [[ -n "$name" && "$name" != *[[:space:]]* ]] || { printf ''; return; }
  printf '%s' "$name"
}

# -----------------------------------------------------------------------
# Internal: discover workload resources in the active subscription.
# Mutates the in-process env (export) for every value it finds.
# -----------------------------------------------------------------------
_az_context_discover_workload_env() {
  local sub
  sub="$(az account show --query id -o tsv 2>/dev/null)" || return 1
  [[ -n "$sub" ]] || return 1

  # Find the workload RG. Search order:
  #   1) AZURE_RESOURCE_GROUP env var (if it exists in the active sub).
  #   2) "rg-elb-dashboard" (the convention for this repo).
  #   3) Any "rg-elb-*" RG that contains an "acrelbdashboard*" ACR.
  local rg=""
  if [[ -n "${AZURE_RESOURCE_GROUP:-}" ]] \
       && az group show -n "$AZURE_RESOURCE_GROUP" --subscription "$sub" -o none 2>/dev/null; then
    rg="$AZURE_RESOURCE_GROUP"
  elif az group show -n rg-elb-dashboard --subscription "$sub" -o none 2>/dev/null; then
    rg=rg-elb-dashboard
  else
    local candidates candidate
    candidates="$(az group list --subscription "$sub" --query "[?starts_with(name, 'rg-elb-')].name" -o tsv 2>/dev/null || true)"
    while IFS= read -r candidate; do
      [[ -z "$candidate" ]] && continue
      if [[ -n "$(az acr list -g "$candidate" --subscription "$sub" --query "[?starts_with(name, 'acrelbdashboard')] | [0].name" -o tsv 2>/dev/null)" ]]; then
        rg="$candidate"
        break
      fi
    done <<< "$candidates"
  fi

  if [[ -z "$rg" ]]; then
    _az_context_warn "could not discover workload RG in subscription $sub (looked for rg-elb-dashboard or rg-elb-* with acrelbdashboard*); falling back to env vars as-is"
    return 1
  fi

  _az_context_log "discovering workload env from rg=$rg sub=$sub"

  local tenant_id="" loc=""
  tenant_id=$(az account show --query tenantId -o tsv 2>/dev/null || printf '')
  loc=$(az group show -n "$rg" --subscription "$sub" --query location -o tsv 2>/dev/null || printf '')

  local acr_name="" acr_login_server=""
  acr_name=$(az acr list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'acrelbdashboard')] | [0].name" -o tsv 2>/dev/null || printf '')
  [[ -z "$acr_name" ]] && acr_name=$(az acr list -g "$rg" --subscription "$sub" --query "[0].name" -o tsv 2>/dev/null || printf '')
  if [[ -n "$acr_name" ]]; then
    acr_login_server=$(az acr show -n "$acr_name" -g "$rg" --subscription "$sub" --query loginServer -o tsv 2>/dev/null || printf '')
  fi

  local app_name="" app_fqdn=""
  app_name=$(az containerapp list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'ca-elb-')] | [0].name" -o tsv 2>/dev/null || printf '')
  [[ -z "$app_name" ]] && app_name=$(az containerapp list -g "$rg" --subscription "$sub" --query "[0].name" -o tsv 2>/dev/null || printf '')
  if [[ -n "$app_name" ]]; then
    app_fqdn=$(az containerapp show -n "$app_name" -g "$rg" --subscription "$sub" --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || printf '')
  fi

  local cae_name=""
  cae_name=$(az containerapp env list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'cae-elb-')] | [0].name" -o tsv 2>/dev/null || printf '')
  [[ -z "$cae_name" ]] && cae_name=$(az containerapp env list -g "$rg" --subscription "$sub" --query "[0].name" -o tsv 2>/dev/null || printf '')

  local storage_name=""
  storage_name=$(az storage account list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'stelbdashboard')] | [0].name" -o tsv 2>/dev/null || printf '')
  [[ -z "$storage_name" ]] && storage_name=$(az storage account list -g "$rg" --subscription "$sub" --query "[0].name" -o tsv 2>/dev/null || printf '')

  local kv_name=""
  kv_name=$(az keyvault list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'kv-elb-')] | [0].name" -o tsv 2>/dev/null || printf '')
  [[ -z "$kv_name" ]] && kv_name=$(az keyvault list -g "$rg" --subscription "$sub" --query "[0].name" -o tsv 2>/dev/null || printf '')

  local law_id=""
  law_id=$(az monitor log-analytics workspace list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'log-elb-')] | [0].id" -o tsv 2>/dev/null || printf '')
  [[ -z "$law_id" ]] && law_id=$(az monitor log-analytics workspace list -g "$rg" --subscription "$sub" --query "[0].id" -o tsv 2>/dev/null || printf '')

  local mi_name="" mi_resource="" mi_client="" mi_principal=""
  mi_name=$(az identity list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'id-elb-')] | [0].name" -o tsv 2>/dev/null || printf '')
  if [[ -n "$mi_name" ]]; then
    mi_resource=$(az identity show -g "$rg" -n "$mi_name" --subscription "$sub" --query id -o tsv 2>/dev/null || printf '')
    mi_client=$(az identity show -g "$rg" -n "$mi_name" --subscription "$sub" --query clientId -o tsv 2>/dev/null || printf '')
    mi_principal=$(az identity show -g "$rg" -n "$mi_name" --subscription "$sub" --query principalId -o tsv 2>/dev/null || printf '')
  fi

  local ai_name="" ai_conn=""
  ai_name=$(az resource list -g "$rg" --subscription "$sub" --resource-type Microsoft.Insights/components --query "[0].name" -o tsv 2>/dev/null || printf '')
  if [[ -n "$ai_name" ]]; then
    ai_conn=$(az monitor app-insights component show -g "$rg" -a "$ai_name" --subscription "$sub" --query connectionString -o tsv 2>/dev/null || printf '')
  fi

  # API_CLIENT_ID is the App Registration client id used by MSAL. In a
  # deployed Container App it can live in three places:
  #   1. Any container's env as a plain value (name in {API_CLIENT_ID,
  #      VITE_AZURE_CLIENT_ID}).
  #   2. Any container's env as a secretRef -> Container App secret.
  #   3. A Container-App-level secret with a conventional name
  #      (api-client-id / apiclientid / vite-azure-client-id).
  # We try each in order so the helper picks up whatever the prior deploy
  # produced (postprovision.sh injects it as a plain env value; cli-upgrade
  # rounds occasionally produce a secretRef form).
  local api_client="" secret_ref=""
  if [[ -n "$app_name" ]]; then
    api_client=$(az containerapp show -n "$app_name" -g "$rg" --subscription "$sub" \
      --query "properties.template.containers[].env[] | [?(name=='API_CLIENT_ID' || name=='VITE_AZURE_CLIENT_ID') && value!=null] | [0].value" \
      -o tsv 2>/dev/null || printf '')
    [[ "$api_client" == "None" ]] && api_client=""

    if [[ -z "$api_client" ]]; then
      secret_ref=$(az containerapp show -n "$app_name" -g "$rg" --subscription "$sub" \
        --query "properties.template.containers[].env[] | [?(name=='API_CLIENT_ID' || name=='VITE_AZURE_CLIENT_ID') && secretRef!=null] | [0].secretRef" \
        -o tsv 2>/dev/null || printf '')
      [[ "$secret_ref" == "None" ]] && secret_ref=""
      if [[ -n "$secret_ref" ]]; then
        api_client=$(az containerapp secret show -n "$app_name" -g "$rg" --subscription "$sub" \
          --secret-name "$secret_ref" --query value -o tsv 2>/dev/null || printf '')
        [[ "$api_client" == "None" ]] && api_client=""
      fi
    fi

    if [[ -z "$api_client" ]]; then
      local sname
      for sname in api-client-id apiclientid vite-azure-client-id azure-client-id; do
        api_client=$(az containerapp secret show -n "$app_name" -g "$rg" --subscription "$sub" \
          --secret-name "$sname" --query value -o tsv 2>/dev/null || printf '')
        [[ "$api_client" == "None" ]] && api_client=""
        [[ -n "$api_client" ]] && break
      done
    fi
  fi

  # Export to in-process env. These values come from authoritative ARM
  # lookups, so we OVERWRITE whatever the shell currently has — that is
  # exactly the point of this helper.
  [[ -n "$tenant_id"        ]] && export AZURE_TENANT_ID="$tenant_id"
  [[ -n "$rg"               ]] && export AZURE_RESOURCE_GROUP="$rg"
  [[ -n "$loc"              ]] && export AZURE_LOCATION="$loc"
  [[ -n "$acr_name"         ]] && export ACR_NAME="$acr_name"
  [[ -n "$acr_login_server" ]] && export ACR_LOGIN_SERVER="$acr_login_server"
  [[ -n "$app_name"         ]] && export CONTAINER_APP_NAME="$app_name"
  [[ -n "$app_fqdn"         ]] && export CONTAINER_APP_FQDN="$app_fqdn"
  [[ -n "$cae_name"         ]] && export CONTAINER_ENV_NAME="$cae_name"
  [[ -n "$storage_name"     ]] && export STORAGE_ACCOUNT_NAME="$storage_name"
  [[ -n "$kv_name"          ]] && export KEY_VAULT_NAME="$kv_name"
  [[ -n "$law_id"           ]] && export LOG_ANALYTICS_WORKSPACE_ID="$law_id"
  [[ -n "$mi_resource"      ]] && export SHARED_IDENTITY_RESOURCE_ID="$mi_resource"
  [[ -n "$mi_client"        ]] && export SHARED_IDENTITY_CLIENT_ID="$mi_client"
  [[ -n "$mi_principal"     ]] && export SHARED_IDENTITY_PRINCIPAL_ID="$mi_principal"
  [[ -n "$ai_conn"          ]] && export APPLICATIONINSIGHTS_CONNECTION_STRING="$ai_conn"
  if [[ -n "$api_client" ]]; then
    export API_CLIENT_ID="$api_client"
    export VITE_AZURE_CLIENT_ID="$api_client"
  fi

  # Persist to the currently selected azd env so the next shell starts
  # aligned without re-running the discovery cost (~5-10 s of ARM calls).
  local azd_env_name
  azd_env_name="$(_az_context_current_azd_env_name)"
  if [[ -n "$azd_env_name" ]]; then
    local k v persisted=0
    for k in AZURE_TENANT_ID AZURE_RESOURCE_GROUP AZURE_LOCATION \
             ACR_NAME ACR_LOGIN_SERVER \
             CONTAINER_APP_NAME CONTAINER_APP_FQDN CONTAINER_ENV_NAME \
             STORAGE_ACCOUNT_NAME KEY_VAULT_NAME LOG_ANALYTICS_WORKSPACE_ID \
             SHARED_IDENTITY_RESOURCE_ID SHARED_IDENTITY_CLIENT_ID SHARED_IDENTITY_PRINCIPAL_ID \
             APPLICATIONINSIGHTS_CONNECTION_STRING \
             API_CLIENT_ID VITE_AZURE_CLIENT_ID; do
      v="${!k:-}"
      if [[ -n "$v" ]]; then
        azd env set "$k" "$v" >/dev/null 2>&1 && persisted=$((persisted + 1)) || true
      fi
    done
    _az_context_log "persisted $persisted values to azd env '$azd_env_name'"
  else
    _az_context_warn "no azd env selected; in-process exports only (rerun won't re-discover until azd env is selected)"
  fi

  _az_context_log "workload env ready: rg=$rg acr=${acr_name:-?} app=${app_name:-?} fqdn=${app_fqdn:-?}"
  return 0
}

# -----------------------------------------------------------------------
# Public entry point. Call once at the top of a manual deploy script.
# -----------------------------------------------------------------------
prepare_deploy_env_from_az_login() {
  local current_sub
  if ! current_sub=$(az account show --query id -o tsv 2>/dev/null); then
    printf '\033[31mERROR:\033[0m Not logged in to Azure CLI. Run: az login\n' >&2
    exit 1
  fi

  local prior_sub="${AZURE_SUBSCRIPTION_ID:-}"
  export AZURE_SUBSCRIPTION_ID="$current_sub"

  if [[ -n "$prior_sub" && "$prior_sub" != "$current_sub" ]]; then
    _az_context_log "subscription mismatch: az login=$current_sub  prior env=$prior_sub  -> using az login"
    # The prior shell env almost certainly carries stale resource names
    # that exist in a different sub. Clear them so discovery starts from
    # a clean slate.
    local v
    for v in "${_AZ_CONTEXT_SUBSCRIPTION_SCOPED_VARS[@]}"; do
      if [[ -n "${!v:-}" ]]; then
        unset "$v"
      fi
    done
    _az_context_log "cleared stale subscription-scoped vars"
  fi

  _az_context_discover_workload_env || true

  # Sanity: discovery must have at least the build/PATCH essentials. If
  # not, the deploy script's own env validation will produce a clearer
  # message than failing later inside az acr build.
  local missing=()
  local v
  for v in AZURE_RESOURCE_GROUP ACR_NAME ACR_LOGIN_SERVER CONTAINER_APP_NAME; do
    [[ -z "${!v:-}" ]] && missing+=("$v")
  done
  if (( ${#missing[@]} > 0 )); then
    _az_context_warn "discovery did not produce: ${missing[*]} — the deploy script may abort on env validation"
  else
    _az_context_log "active subscription: $current_sub  rg=$AZURE_RESOURCE_GROUP  acr=$ACR_NAME  app=$CONTAINER_APP_NAME"
  fi
}

# -----------------------------------------------------------------------
# Compatibility shim. The previous public name is still exported so the
# call sites in quick-deploy.sh / cli-upgrade.sh keep working without
# requiring a coordinated rename.
# -----------------------------------------------------------------------
assert_az_subscription_aligned() {
  prepare_deploy_env_from_az_login
}
