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
  # The [8/9] step calls `az monitor app-insights component show`, which lives
  # in the `application-insights` az extension. On a profile/CI where that
  # extension is not yet installed, az CLI's dynamic-install defaults to
  # `yes_prompt` and reads y/n from STDIN. The `2>/dev/null` on each call hides
  # the prompt text but the stdin read still BLOCKS, so a non-interactive deploy
  # hangs forever at "[8/9] managed identity + app insights" (the `|| printf ''`
  # never fires because the process never exits). Force a non-interactive
  # auto-install for the duration of discovery so the call completes instead of
  # waiting on input; respect any value the operator pre-set.
  export AZURE_EXTENSION_USE_DYNAMIC_INSTALL="${AZURE_EXTENSION_USE_DYNAMIC_INSTALL:-yes_without_prompt}"

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
  _az_context_log "  (this runs ~18 serial ARM lookups; ~15s warm, up to ~60s cold)"

  local tenant_id="" loc=""
  _az_context_log "  [1/9] tenant + location"
  tenant_id=$(az account show --query tenantId -o tsv 2>/dev/null || printf '')
  loc=$(az group show -n "$rg" --subscription "$sub" --query location -o tsv 2>/dev/null || printf '')

  local acr_name="" acr_login_server=""
  _az_context_log "  [2/9] container registry"
  acr_name=$(az acr list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'acrelbdashboard')] | [0].name" -o tsv 2>/dev/null || printf '')
  [[ -z "$acr_name" ]] && acr_name=$(az acr list -g "$rg" --subscription "$sub" --query "[0].name" -o tsv 2>/dev/null || printf '')
  if [[ -n "$acr_name" ]]; then
    acr_login_server=$(az acr show -n "$acr_name" -g "$rg" --subscription "$sub" --query loginServer -o tsv 2>/dev/null || printf '')
  fi

  local app_name="" app_fqdn=""
  _az_context_log "  [3/9] container app"
  app_name=$(az containerapp list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'ca-elb-')] | [0].name" -o tsv 2>/dev/null || printf '')
  [[ -z "$app_name" ]] && app_name=$(az containerapp list -g "$rg" --subscription "$sub" --query "[0].name" -o tsv 2>/dev/null || printf '')
  if [[ -n "$app_name" ]]; then
    app_fqdn=$(az containerapp show -n "$app_name" -g "$rg" --subscription "$sub" --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || printf '')
  fi

  local cae_name=""
  _az_context_log "  [4/9] container app environment"
  cae_name=$(az containerapp env list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'cae-elb-')] | [0].name" -o tsv 2>/dev/null || printf '')
  [[ -z "$cae_name" ]] && cae_name=$(az containerapp env list -g "$rg" --subscription "$sub" --query "[0].name" -o tsv 2>/dev/null || printf '')

  local storage_name=""
  _az_context_log "  [5/9] storage account"
  storage_name=$(az storage account list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'stelbdashboard')] | [0].name" -o tsv 2>/dev/null || printf '')
  [[ -z "$storage_name" ]] && storage_name=$(az storage account list -g "$rg" --subscription "$sub" --query "[0].name" -o tsv 2>/dev/null || printf '')

  local kv_name=""
  _az_context_log "  [6/9] key vault"
  kv_name=$(az keyvault list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'kv-elb-')] | [0].name" -o tsv 2>/dev/null || printf '')
  [[ -z "$kv_name" ]] && kv_name=$(az keyvault list -g "$rg" --subscription "$sub" --query "[0].name" -o tsv 2>/dev/null || printf '')

  local law_id=""
  _az_context_log "  [7/9] log analytics workspace"
  law_id=$(az monitor log-analytics workspace list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'log-elb-')] | [0].id" -o tsv 2>/dev/null || printf '')
  [[ -z "$law_id" ]] && law_id=$(az monitor log-analytics workspace list -g "$rg" --subscription "$sub" --query "[0].id" -o tsv 2>/dev/null || printf '')

  local mi_name="" mi_resource="" mi_client="" mi_principal=""
  _az_context_log "  [8/9] managed identity + app insights"
  mi_name=$(az identity list -g "$rg" --subscription "$sub" --query "[?starts_with(name, 'id-elb-')] | [0].name" -o tsv 2>/dev/null || printf '')
  if [[ -n "$mi_name" ]]; then
    mi_resource=$(az identity show -g "$rg" -n "$mi_name" --subscription "$sub" --query id -o tsv 2>/dev/null || printf '')
    mi_client=$(az identity show -g "$rg" -n "$mi_name" --subscription "$sub" --query clientId -o tsv 2>/dev/null || printf '')
    mi_principal=$(az identity show -g "$rg" -n "$mi_name" --subscription "$sub" --query principalId -o tsv 2>/dev/null || printf '')
  fi

  local ai_name="" ai_conn=""
  ai_name=$(az resource list -g "$rg" --subscription "$sub" --resource-type Microsoft.Insights/components --query "[0].name" -o tsv 2>/dev/null </dev/null || printf '')
  if [[ -n "$ai_name" ]]; then
    # `</dev/null` is belt-and-suspenders alongside AZURE_EXTENSION_USE_DYNAMIC_INSTALL
    # above: if any az prompt still fires here, it reads EOF and fails fast
    # instead of blocking the deploy on stdin.
    ai_conn=$(az monitor app-insights component show -g "$rg" -a "$ai_name" --subscription "$sub" --query connectionString -o tsv 2>/dev/null </dev/null || printf '')
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
  _az_context_log "  [9/9] MSAL app registration client id"
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
  # Capture an operator-supplied ACR_NAME override BEFORE discovery clears +
  # re-derives it from the active subscription. Naming a specific ACR pins the
  # intended subscription (ACR names are globally unique), so a mismatch with
  # the active sub's ACR after discovery means the deploy would target the
  # wrong environment — the cross-sub ACR guard at the end of this function
  # turns that silent retarget into a loud refusal.
  local _entry_acr_override="${ACR_NAME:-}"
  if ! current_sub=$(az account show --query id -o tsv 2>/dev/null); then
    printf '\033[31mERROR:\033[0m Not logged in to Azure CLI. Run: az login\n' >&2
    exit 1
  fi

  # Token-freshness preflight. `az account show` reads cached account
  # metadata and succeeds even when the access/refresh token has expired.
  # If we skip this, the FIRST ARM lookup inside discovery is what trips
  # the expiry — and in a non-TTY deploy shell `az` then blocks forever
  # waiting on an interactive re-auth prompt that nobody can answer, which
  # is exactly the "stuck at discovering workload env" symptom. Force a
  # real token acquisition up front (bounded by `timeout` so a wedged auth
  # broker can't hang here either) and, if it fails, tell the operator to
  # run `az login` instead of silently hanging later.
  _az_context_log "verifying az login token is valid ..."
  local token_check_rc=0
  if command -v timeout >/dev/null 2>&1; then
    timeout 25s az account get-access-token --output none </dev/null >/dev/null 2>&1 || token_check_rc=$?
  else
    az account get-access-token --output none </dev/null >/dev/null 2>&1 || token_check_rc=$?
  fi
  if [[ "$token_check_rc" -ne 0 ]]; then
    printf '\033[31mERROR:\033[0m Azure CLI login has expired or needs re-authentication.\n' >&2
    if [[ "$token_check_rc" -eq 124 ]]; then
      printf '       (token check timed out after 25s — the auth broker did not respond)\n' >&2
    fi
    printf '       Re-authenticate, then re-run this deploy:\n' >&2
    printf '         az login\n' >&2
    printf '       (device-code login if no browser is available: az login --use-device-code)\n' >&2
    exit 1
  fi

  # Sub-mismatch guard. Two Container Apps in two different subs can
  # easily share the same name (`ca-elb-dashboard` lives in both the
  # company prod sub and a teammate's MCAP sub). Without this guard,
  # quick-deploy.sh discovers the active-sub Container App + ACR by
  # name and pushes to the wrong place — operator just sees green
  # "Done" while the user-facing prod is unchanged (see the 2026-05-28
  # incident). Forcing an explicit override on every cross-sub deploy
  # makes the mistake noisy instead of silent.
  if command -v azd >/dev/null 2>&1; then
    local azd_sub azd_values
    # Guard the azd call the same way lib-env.sh::load_azd_env does: a slow,
    # unauthenticated, or prompting `azd` (e.g. "Select an environment") would
    # otherwise hang the WHOLE deploy indefinitely here — observed blocking
    # quick-deploy at the sub-mismatch guard with no output. `timeout 8s` +
    # `</dev/null` make it fail fast (no env discovered → skip the guard) so an
    # explicit-override deploy is never held hostage by a wedged azd CLI.
    if command -v timeout >/dev/null 2>&1; then
      azd_values="$(timeout 8s azd env get-values </dev/null 2>/dev/null || true)"
    else
      azd_values="$(azd env get-values </dev/null 2>/dev/null || true)"
    fi
    azd_sub="$(printf '%s\n' "$azd_values" \
      | awk -F'=' '/^AZURE_SUBSCRIPTION_ID=/{gsub(/"/,"",$2); print $2; exit}')"
    if [[ -n "$azd_sub" && "$azd_sub" != "$current_sub" ]]; then
      if [[ "${ELB_ALLOW_SUB_MISMATCH:-0}" != "1" ]]; then
        printf '\033[31mERROR:\033[0m az login sub (%s) does NOT match azd env sub (%s).\n' \
          "$current_sub" "$azd_sub" >&2
        printf '       Deploying now would target the az-login sub, which is almost certainly NOT\n' >&2
        printf '       the environment your azd env points at. To proceed anyway, re-run with:\n' >&2
        printf '         ELB_ALLOW_SUB_MISMATCH=1 %s\n' "$0 $*" >&2
        printf '       Or align them first:\n' >&2
        printf '         az account set --subscription %s   # use azd env sub\n' "$azd_sub" >&2
        printf '         azd env select <env-for-%s>        # use az login sub\n' "$current_sub" >&2
        exit 2
      fi
      _az_context_warn "sub mismatch acknowledged via ELB_ALLOW_SUB_MISMATCH=1; deploying to az login sub $current_sub"
    fi
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

  # Cross-sub ACR override guard (a DIFFERENT axis than the azd-vs-login guard
  # above — ELB_ALLOW_SUB_MISMATCH does NOT bypass it). An operator who passes
  # ACR_NAME=<X> is naming a specific registry, which pins the subscription
  # they intend to deploy to. Discovery just overwrote ACR_NAME with the ACTIVE
  # subscription's registry; if that differs from <X>, the active sub is not the
  # one that owns <X>, so `az acr build` would push to <X> while
  # `az containerapp update` PATCHES the active sub's Container App — a
  # wrong-environment deploy (observed 2026-06-20: active sub silently flipped,
  # an ACR_NAME override pointed at the customer registry, the patch nearly hit
  # the teammate sub's identically-named app). Refuse unless the operator truly
  # means to build cross-sub.
  if [[ -n "$_entry_acr_override" && -n "${ACR_NAME:-}" \
        && "$_entry_acr_override" != "$ACR_NAME" ]]; then
    if [[ "${ELB_ALLOW_ACR_OVERRIDE_MISMATCH:-0}" != "1" ]]; then
      printf '\033[31mERROR:\033[0m ACR_NAME override (%s) does NOT match the active subscription'\''s ACR (%s).\n' \
        "$_entry_acr_override" "$ACR_NAME" >&2
      printf '       The active sub (%s) does not own %s, so this deploy would PATCH the WRONG\n' \
        "$current_sub" "$_entry_acr_override" >&2
      printf '       environment'\''s Container App. Align the active sub to the one that owns the ACR:\n' >&2
      printf '         az account set --subscription <sub-that-owns-%s>\n' "$_entry_acr_override" >&2
      printf '       then re-run. To force a cross-sub build anyway: ELB_ALLOW_ACR_OVERRIDE_MISMATCH=1\n' >&2
      exit 3
    fi
    _az_context_warn "ACR override mismatch acknowledged via ELB_ALLOW_ACR_OVERRIDE_MISMATCH=1 (override=$_entry_acr_override active-sub-acr=$ACR_NAME)"
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
