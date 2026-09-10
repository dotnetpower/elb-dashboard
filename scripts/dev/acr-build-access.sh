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
  local subscription_args=()
  [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]] && subscription_args=(--subscription "$AZURE_SUBSCRIPTION_ID")
  az acr show \
    --name "$acr_name" \
    "${subscription_args[@]}" \
    --query '{public: publicNetworkAccess, defaultAction: networkRuleSet.defaultAction, trusted: networkRuleBypassOptions}' \
    -o tsv
}

acr_capture_build_access_state() {
  local acr_name="${1:?acr name required}"
  local state
  if ! state="$(acr_show_network_state "$acr_name")"; then
    acr_build_access_log "ERROR: could not read ACR network policy for $acr_name"
    return 1
  fi
  read -r ACR_BUILD_ACCESS_ORIGINAL_PUBLIC \
          ACR_BUILD_ACCESS_ORIGINAL_DEFAULT_ACTION \
          ACR_BUILD_ACCESS_ORIGINAL_BYPASS <<< "$state"
  if [[ ! "$ACR_BUILD_ACCESS_ORIGINAL_PUBLIC" =~ ^(Enabled|Disabled)$ || \
        ! "$ACR_BUILD_ACCESS_ORIGINAL_DEFAULT_ACTION" =~ ^(Allow|Deny)$ || \
      ! "$ACR_BUILD_ACCESS_ORIGINAL_BYPASS" =~ ^(AzureServices|None)$ ]]; then
    acr_build_access_log "ERROR: ACR network policy response was incomplete for $acr_name"
    return 1
  fi
}

acr_wait_for_build_access_state() {
  local acr_name="${1:?acr name required}"
  local max_attempts="${ACR_BUILD_ACCESS_READY_ATTEMPTS:-18}"
  local interval_seconds="${ACR_BUILD_ACCESS_READY_INTERVAL_SECONDS:-5}"
  local attempt public_state default_action bypass

  if [[ ! "$max_attempts" =~ ^[0-9]+$ || "$max_attempts" -lt 1 || "$max_attempts" -gt 120 ]]; then
    acr_build_access_log "ERROR: ACR_BUILD_ACCESS_READY_ATTEMPTS must be between 1 and 120"
    return 2
  fi
  if [[ ! "$interval_seconds" =~ ^[0-9]+$ || "$interval_seconds" -gt 300 ]]; then
    acr_build_access_log "ERROR: ACR_BUILD_ACCESS_READY_INTERVAL_SECONDS must be between 0 and 300"
    return 2
  fi

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
  local subscription_args=()
  [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]] && subscription_args=(--subscription "$AZURE_SUBSCRIPTION_ID")

  acr_capture_build_access_state "$acr_name" || return 1
  ACR_BUILD_ACCESS_RESTORE_NEEDED=0
  ACR_BUILD_ACCESS_RESTORE_TO_STEADY_STATE=0

  if [[ "$ACR_BUILD_ACCESS_ORIGINAL_PUBLIC" != "Enabled" || \
        "$ACR_BUILD_ACCESS_ORIGINAL_DEFAULT_ACTION" != "Allow" || \
        "$ACR_BUILD_ACCESS_ORIGINAL_BYPASS" != "AzureServices" ]]; then
    acr_build_access_log "==> Opening ACR build access temporarily (public=Enabled, defaultAction=Allow, trustedServices=true)"
    # Arm the lease before the mutation. The ARM operation may succeed even if
    # the CLI loses its response, and the caller's EXIT trap must still restore.
    ACR_BUILD_ACCESS_RESTORE_NEEDED=1
    if ! az acr update \
      --name "$acr_name" \
      "${subscription_args[@]}" \
      --public-network-enabled true \
      --default-action Allow \
      --allow-trusted-services true \
      -o none >/dev/null; then
      acr_build_access_log "ERROR: failed to open ACR build access"
      return 1
    fi
    acr_wait_for_build_access_state "$acr_name" || return 1
    acr_build_access_log "    ACR policy accepted; settling ${settle_seconds}s for build-agent propagation"
    sleep "$settle_seconds"
  else
    case "${ACR_BUILD_ACCESS_PRESERVE_OPEN:-}" in
      1|true|TRUE|yes|YES)
        acr_build_access_log "==> ACR build access already open; explicit preserve requested"
        ;;
      *)
        # The source-of-truth steady state is private-only. A previous killed
        # deploy can strand the temporary build posture at Enabled/Allow; the
        # old helper then treated that incident state as intentional forever.
        # Mark it for an idle-checked close after this process's builds finish.
        # The active-run check in `acr_restore_build_access` prevents one
        # deploy from cutting off another caller's concurrent ACR Task.
        acr_build_access_log "==> ACR build access already open; will restore private steady state when builds are idle"
        ACR_BUILD_ACCESS_RESTORE_NEEDED=1
        ACR_BUILD_ACCESS_RESTORE_TO_STEADY_STATE=1
        ;;
    esac
  fi
}

acr_active_build_count() {
  local acr_name="${1:?acr name required}"
  local subscription_args=()
  [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]] && subscription_args=(--subscription "$AZURE_SUBSCRIPTION_ID")
  timeout 30s az acr task list-runs \
    --registry "$acr_name" \
    "${subscription_args[@]}" \
    --top 100 \
    --query "length([?status=='Queued' || status=='Started' || status=='Running'])" \
    -o tsv 2>/dev/null
}

acr_restore_build_access() {
  local acr_name="${1:-}"
  [[ -n "$acr_name" ]] || return 0
  [[ "${ACR_BUILD_ACCESS_RESTORE_NEEDED:-0}" == "1" ]] || return 0

  local active_builds
  if ! active_builds="$(acr_active_build_count "$acr_name")" || \
     [[ ! "$active_builds" =~ ^[0-9]+$ ]]; then
    acr_build_access_log "WARN: could not verify active ACR builds; leaving build access open"
    return 0
  fi
  if (( active_builds > 0 )); then
    acr_build_access_log "WARN: ${active_builds} other ACR build(s) still active; leaving build access open"
    return 0
  fi

  if [[ "${ACR_BUILD_ACCESS_RESTORE_TO_STEADY_STATE:-0}" == "1" ]]; then
    ACR_BUILD_ACCESS_ORIGINAL_PUBLIC=Disabled
    ACR_BUILD_ACCESS_ORIGINAL_DEFAULT_ACTION=Deny
    ACR_BUILD_ACCESS_ORIGINAL_BYPASS=AzureServices
  fi

  local public_enabled trusted_services subscription_args=()
  [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]] && subscription_args=(--subscription "$AZURE_SUBSCRIPTION_ID")
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
  if ! az acr update \
    --name "$acr_name" \
    "${subscription_args[@]}" \
    --public-network-enabled "$public_enabled" \
    --default-action "${ACR_BUILD_ACCESS_ORIGINAL_DEFAULT_ACTION:-Deny}" \
    --allow-trusted-services "$trusted_services" \
    -o none >/dev/null 2>&1; then
    acr_build_access_log "ERROR: failed to restore ACR network policy"
    return 1
  fi
  ACR_BUILD_ACCESS_RESTORE_NEEDED=0
  ACR_BUILD_ACCESS_RESTORE_TO_STEADY_STATE=0
}

acr_private_steady_state_ready() {
  local acr_name="${1:?acr name required}"
  local public_state default_action bypass state
  state="$(acr_show_network_state "$acr_name")" || return 1
  read -r public_state default_action bypass <<< "$state"
  [[ "$public_state" == "Disabled" && \
     "$default_action" == "Deny" && \
     "$bypass" == "AzureServices" ]]
}

acr_reconcile_private_steady_state() {
  local acr_name="${1:?acr name required}"
  local max_attempts="${ACR_BUILD_ACCESS_IDLE_ATTEMPTS:-1}"
  local interval_seconds="${ACR_BUILD_ACCESS_IDLE_INTERVAL_SECONDS:-10}"
  local verify_attempts="${ACR_BUILD_ACCESS_PRIVATE_VERIFY_ATTEMPTS:-18}"
  local verify_interval_seconds="${ACR_BUILD_ACCESS_PRIVATE_VERIFY_INTERVAL_SECONDS:-5}"
  local attempt active_builds

  if [[ ! "$max_attempts" =~ ^[0-9]+$ || "$max_attempts" -lt 1 || "$max_attempts" -gt 360 ]]; then
    acr_build_access_log "ERROR: ACR_BUILD_ACCESS_IDLE_ATTEMPTS must be between 1 and 360"
    return 2
  fi
  if [[ ! "$interval_seconds" =~ ^[0-9]+$ || "$interval_seconds" -gt 300 ]]; then
    acr_build_access_log "ERROR: ACR_BUILD_ACCESS_IDLE_INTERVAL_SECONDS must be between 0 and 300"
    return 2
  fi
  if [[ ! "$verify_attempts" =~ ^[0-9]+$ || "$verify_attempts" -lt 1 || "$verify_attempts" -gt 120 ]]; then
    acr_build_access_log "ERROR: ACR_BUILD_ACCESS_PRIVATE_VERIFY_ATTEMPTS must be between 1 and 120"
    return 2
  fi
  if [[ ! "$verify_interval_seconds" =~ ^[0-9]+$ || \
        "$verify_interval_seconds" -gt 300 ]]; then
    acr_build_access_log "ERROR: ACR_BUILD_ACCESS_PRIVATE_VERIFY_INTERVAL_SECONDS must be between 0 and 300"
    return 2
  fi

  if acr_private_steady_state_ready "$acr_name"; then
    acr_build_access_log "ACR network policy already matches private steady state"
    return 0
  fi

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    if active_builds="$(acr_active_build_count "$acr_name")" && \
       [[ "$active_builds" =~ ^[0-9]+$ ]]; then
      if (( active_builds == 0 )); then
        ACR_BUILD_ACCESS_RESTORE_NEEDED=1
        ACR_BUILD_ACCESS_RESTORE_TO_STEADY_STATE=1
        if ! acr_restore_build_access "$acr_name"; then
          acr_build_access_log "ERROR: ACR private restore request failed"
          return 1
        fi
        for ((verify = 1; verify <= verify_attempts; verify++)); do
          if acr_private_steady_state_ready "$acr_name"; then
            acr_build_access_log "ACR private steady state verified"
            return 0
          fi
          (( verify < verify_attempts )) && sleep "$verify_interval_seconds"
        done
        acr_build_access_log "ERROR: ACR private steady state did not become effective"
        return 1
      fi
      acr_build_access_log "Waiting for ${active_builds} active ACR build(s) before private restore (${attempt}/${max_attempts})"
    else
      acr_build_access_log "WARN: could not verify active ACR builds (${attempt}/${max_attempts})"
    fi
    (( attempt < max_attempts )) && sleep "$interval_seconds"
  done

  acr_build_access_log "ERROR: ACR builds did not become idle before the restore deadline"
  return 1
}
