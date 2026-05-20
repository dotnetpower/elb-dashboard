#!/usr/bin/env bash
# Shared ACR network-policy guard for `az acr build`.
#
# Workload Storage accounts must stay private; this helper is only for the
# deployment ACR. ACR Tasks run from Microsoft-managed build agents, and after
# switching `publicNetworkAccess/defaultAction` there is a short propagation
# window before those agents can log in to the registry. Centralise the policy
# here so deploy scripts open, verify, settle, and restore the registry the same
# way every time.

acr_build_access_log() {
  if declare -F ts >/dev/null 2>&1; then
    ts "$*"
  else
    printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"
  fi
}

acr_show_network_state() {
  local acr_name="${1:?acr name required}"
  az acr show \
    --name "$acr_name" \
    --query '{public: publicNetworkAccess, defaultAction: networkRuleSet.defaultAction, trusted: networkRuleBypassOptions}' \
    -o tsv
}

acr_capture_build_access_state() {
  local acr_name="${1:?acr name required}"
  local state
  state="$(acr_show_network_state "$acr_name")"
  read -r ACR_BUILD_ACCESS_ORIGINAL_PUBLIC \
          ACR_BUILD_ACCESS_ORIGINAL_DEFAULT_ACTION \
          ACR_BUILD_ACCESS_ORIGINAL_BYPASS <<< "$state"
  ACR_BUILD_ACCESS_ORIGINAL_PUBLIC="${ACR_BUILD_ACCESS_ORIGINAL_PUBLIC:-Disabled}"
  ACR_BUILD_ACCESS_ORIGINAL_DEFAULT_ACTION="${ACR_BUILD_ACCESS_ORIGINAL_DEFAULT_ACTION:-Deny}"
  ACR_BUILD_ACCESS_ORIGINAL_BYPASS="${ACR_BUILD_ACCESS_ORIGINAL_BYPASS:-AzureServices}"
}

acr_wait_for_build_access_state() {
  local acr_name="${1:?acr name required}"
  local max_attempts="${ACR_BUILD_ACCESS_READY_ATTEMPTS:-18}"
  local interval_seconds="${ACR_BUILD_ACCESS_READY_INTERVAL_SECONDS:-5}"
  local attempt public_state default_action bypass

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    read -r public_state default_action bypass <<< "$(acr_show_network_state "$acr_name")"
    if [[ "$public_state" == "Enabled" && "$default_action" == "Allow" && "$bypass" == "AzureServices" ]]; then
      return 0
    fi
    acr_build_access_log "    waiting for ACR build access policy: public=$public_state default=$default_action trusted=$bypass"
    sleep "$interval_seconds"
  done

  acr_build_access_log "ERROR: ACR build access policy did not become effective in time"
  return 1
}

acr_ensure_build_access() {
  local acr_name="${1:?acr name required}"
  local settle_seconds="${ACR_BUILD_ACCESS_SETTLE_SECONDS:-75}"

  acr_capture_build_access_state "$acr_name"
  ACR_BUILD_ACCESS_RESTORE_NEEDED=0

  if [[ "$ACR_BUILD_ACCESS_ORIGINAL_PUBLIC" != "Enabled" || \
        "$ACR_BUILD_ACCESS_ORIGINAL_DEFAULT_ACTION" != "Allow" || \
        "$ACR_BUILD_ACCESS_ORIGINAL_BYPASS" != "AzureServices" ]]; then
    acr_build_access_log "==> Opening ACR build access temporarily (public=Enabled, defaultAction=Allow, trustedServices=true)"
    az acr update \
      --name "$acr_name" \
      --public-network-enabled true \
      --default-action Allow \
      --allow-trusted-services true \
      -o none >/dev/null
    ACR_BUILD_ACCESS_RESTORE_NEEDED=1
    acr_wait_for_build_access_state "$acr_name"
    acr_build_access_log "    ACR policy accepted; settling ${settle_seconds}s for build-agent propagation"
    sleep "$settle_seconds"
  else
    acr_build_access_log "==> ACR build access already open; leaving current policy unchanged"
  fi
}

acr_restore_build_access() {
  local acr_name="${1:-}"
  [[ -n "$acr_name" ]] || return 0
  [[ "${ACR_BUILD_ACCESS_RESTORE_NEEDED:-0}" == "1" ]] || return 0

  local public_enabled trusted_services
  if [[ "${ACR_BUILD_ACCESS_ORIGINAL_PUBLIC:-Disabled}" == "Enabled" ]]; then
    public_enabled=true
  else
    public_enabled=false
  fi
  if [[ "${ACR_BUILD_ACCESS_ORIGINAL_BYPASS:-AzureServices}" == "AzureServices" ]]; then
    trusted_services=true
  else
    trusted_services=false
  fi

  acr_build_access_log "==> Restoring ACR network policy: public=${ACR_BUILD_ACCESS_ORIGINAL_PUBLIC:-Disabled}, defaultAction=${ACR_BUILD_ACCESS_ORIGINAL_DEFAULT_ACTION:-Deny}, trustedServices=$trusted_services"
  az acr update \
    --name "$acr_name" \
    --public-network-enabled "$public_enabled" \
    --default-action "${ACR_BUILD_ACCESS_ORIGINAL_DEFAULT_ACTION:-Deny}" \
    --allow-trusted-services "$trusted_services" \
    -o none >/dev/null 2>&1 || acr_build_access_log "WARN: failed to restore ACR network policy"
  ACR_BUILD_ACCESS_RESTORE_NEEDED=0
}
