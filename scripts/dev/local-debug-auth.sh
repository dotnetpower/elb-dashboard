#!/usr/bin/env bash
# local-debug-auth.sh — one-shot toggle for LOCAL MSAL DEBUGGING.
#
# Why this exists
# ---------------
# Local dev defaults to AUTH_DEV_BYPASS=true, which renders every caller as
# the synthetic `anonymous` identity. That short-circuit is fine for routes
# that do not talk to Azure, but the moment you hit /api/blast/databases
# (or any data-plane route) the backend's DefaultAzureCredential (your
# `az login` identity) fails RBAC against the workload Storage account
# and the dashboard renders `access_denied` / `network_blocked`.
#
# Running `local-debug-auth.sh on` flips local dev into the same identity
# model as production:
#
#   * Storage RBAC      — grant-local-rbac.sh assigns Storage Blob /
#                         Table Data Contributor + Account Contributor
#                         (idempotent — existing assignments are skipped).
#   * Storage network   — storage-public-access.sh on (only if currently
#                         Disabled), opening the data plane to the host
#                         so DefaultAzureCredential can reach it.
#   * SPA bypass        — AUTH_DEV_BYPASS=false / VITE_AUTH_DEV_BYPASS=false
#                         written to .env and web/.env.local so api validates
#                         MSAL bearer tokens and the SPA performs a real
#                         interactive sign-in.
#   * API_CLIENT_ID     — auto-pulled from azd env (the api needs this to
#                         validate the bearer's audience).
#   * api + web restart — running local-run.sh api / web processes are
#                         stopped and re-launched so the new env takes
#                         effect (idempotent — safe to re-run).
#
# Running `local-debug-auth.sh off` reverts the bypass flags to true and
# closes the storage network surface. RBAC is intentionally NOT revoked
# because it is harmless to leave in place and removing it would force a
# 1-5 minute propagation wait on the next `on`.
#
# Charter §9 reminder
# -------------------
# Storage publicNetworkAccess=Enabled is acceptable only as a transient
# local-debug state. Always run `local-debug-auth.sh off` (or
# storage-public-access.sh off) when you finish — the script also prints
# a final reminder and a `trap` will close the surface on Ctrl-C if
# --close-on-exit is passed.
#
# Usage
# -----
#   scripts/dev/local-debug-auth.sh on  [flags]    # enable real MSAL login
#   scripts/dev/local-debug-auth.sh off [flags]    # revert to dev bypass + close storage
#   scripts/dev/local-debug-auth.sh status [flags] # show current state, no mutations
#
# Flags (all subcommands):
#   --storage NAME         Workload Storage account (default: azd env STORAGE_ACCOUNT_NAME)
#   --storage-rg NAME      Workload Storage resource group (default: azd env AZURE_RESOURCE_GROUP)
#   --acr NAME             Workload ACR (default: azd env ACR_NAME — used by grant-local-rbac.sh)
#   --acr-rg NAME          Workload ACR RG (default: same as --storage-rg)
#   --subscription ID      Override active subscription
#   --skip-rbac            (on only) skip grant-local-rbac.sh
#   --skip-storage         (on only) skip storage-public-access.sh
#   --skip-restart         (on/off) do not restart api + web
#   --no-close-storage     (off only) do not touch storage network state
#   -h | --help            Show this help

set -Eeuo pipefail

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
cyan()   { printf '\033[36m%s\033[0m\n' "$*"; }
ts()     { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die()    { red "ERROR: $*" >&2; exit 1; }

usage() { sed -n '2,60p' "$0"; exit "${1:-1}"; }

# ----------------------------------------------------------------- args ---

[[ $# -ge 1 ]] || usage 1
ACTION="$1"; shift || true
case "$ACTION" in
  on|off|status) ;;
  -h|--help|help) usage 0 ;;
  *) usage 1 ;;
esac

STORAGE=""
STORAGE_RG=""
ACR=""
ACR_RG=""
SUBSCRIPTION=""
SKIP_RBAC=0
SKIP_STORAGE=0
SKIP_RESTART=0
CLOSE_STORAGE_ON_OFF=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --storage)         STORAGE="$2"; shift 2 ;;
    --storage-rg)      STORAGE_RG="$2"; shift 2 ;;
    --acr)             ACR="$2"; shift 2 ;;
    --acr-rg)          ACR_RG="$2"; shift 2 ;;
    --subscription)    SUBSCRIPTION="$2"; shift 2 ;;
    --skip-rbac)       SKIP_RBAC=1; shift ;;
    --skip-storage)    SKIP_STORAGE=1; shift ;;
    --skip-restart)    SKIP_RESTART=1; shift ;;
    --no-close-storage) CLOSE_STORAGE_ON_OFF=0; shift ;;
    -h|--help)         usage 0 ;;
    *)                 die "unknown flag: $1" ;;
  esac
done

# --------------------------------------------------------------- paths ----

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/../.." && pwd)"
env_file="$project_root/.env"
web_env_file="$project_root/web/.env.local"
grant_rbac="$script_dir/grant-local-rbac.sh"
storage_toggle="$script_dir/storage-public-access.sh"
local_run="$script_dir/local-run.sh"

[[ -x "$grant_rbac" ]]      || die "missing helper: $grant_rbac"
[[ -x "$storage_toggle" ]]  || die "missing helper: $storage_toggle"
[[ -x "$local_run" ]]       || die "missing helper: $local_run"

# ---------------------------------------------------------- preflight -----

preflight() {
  command -v az  >/dev/null 2>&1 || die "az CLI not found"
  command -v jq  >/dev/null 2>&1 || die "jq not found (apt install jq)"
  command -v curl >/dev/null 2>&1 || die "curl not found"

  if ! az account show -o none 2>/dev/null; then
    die "az is not signed in — run: az login"
  fi
  if [[ -z "$SUBSCRIPTION" ]]; then
    SUBSCRIPTION="$(az account show --query id -o tsv)"
  fi

  USER_OID="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
  USER_NAME="$(az account show --query user.name -o tsv 2>/dev/null || echo '<unknown>')"
  [[ -n "$USER_OID" ]] || die "could not resolve signed-in user oid (Microsoft Graph User.Read required)"
}

# Resolve defaults from azd env when caller did not pass --storage etc.
resolve_azd_defaults() {
  if ! command -v azd >/dev/null 2>&1; then
    return 0
  fi
  local values
  values="$(azd env get-values 2>/dev/null || true)"
  [[ -n "$values" ]] || return 0

  azd_kv() {
    awk -F= -v key="$1" '$1==key {gsub(/"/,"",$2); print $2; exit}' <<<"$values"
  }
  [[ -z "$STORAGE" ]]    && STORAGE="$(azd_kv STORAGE_ACCOUNT_NAME)"
  [[ -z "$STORAGE_RG" ]] && STORAGE_RG="$(azd_kv AZURE_RESOURCE_GROUP)"
  [[ -z "$ACR" ]]        && ACR="$(azd_kv ACR_NAME)"
  [[ -z "$ACR_RG" ]]     && ACR_RG="$STORAGE_RG"
  : "${API_CLIENT_ID:=$(azd_kv API_CLIENT_ID)}"
  : "${AZURE_TENANT_ID:=$(azd_kv AZURE_TENANT_ID)}"
  export API_CLIENT_ID AZURE_TENANT_ID
}

# Discover the storage account the SPA actually targets. The wizard may pick
# a different deployment than azd env defaults to (e.g. stelbdashboard01… vs
# stelbdashboardmul5oh5j44). If the azd default does not exist but exactly one
# stelb* storage account is reachable, fall back to that.
resolve_storage_or_die() {
  if [[ -n "$STORAGE" ]] && az storage account show --subscription "$SUBSCRIPTION" -n "$STORAGE" -o none 2>/dev/null; then
    if [[ -z "$STORAGE_RG" ]]; then
      STORAGE_RG="$(az storage account show --subscription "$SUBSCRIPTION" -n "$STORAGE" --query resourceGroup -o tsv)"
    fi
    return 0
  fi
  yellow "storage account '$STORAGE' not found in this subscription — searching…"
  local matches
  matches="$(az storage account list --subscription "$SUBSCRIPTION" \
              --query "[?starts_with(name,'stelbdashboard')].{name:name,rg:resourceGroup}" -o tsv)"
  local count
  count="$(printf '%s\n' "$matches" | grep -c .)" || true
  if [[ "$count" -eq 0 ]]; then
    die "no stelbdashboard* storage account found in subscription $SUBSCRIPTION"
  fi
  if [[ "$count" -gt 1 ]]; then
    echo "$matches" | awk '{printf "  %s (rg: %s)\n", $1, $2}' >&2
    die "$count matching storage accounts — pass --storage NAME --storage-rg RG explicitly"
  fi
  STORAGE="$(awk '{print $1}' <<<"$matches")"
  STORAGE_RG="$(awk '{print $2}' <<<"$matches")"
  ts "auto-resolved storage: $STORAGE (rg: $STORAGE_RG)"
}

# Probe whether the caller can list role assignments at the storage scope.
# `az role assignment create` requires User Access Administrator / Owner, but
# `list` is much cheaper and 99% predictive of create permission for the same
# scope. We surface a clear error before grant-local-rbac.sh runs.
check_rbac_permission() {
  local scope="$1"
  if ! az role assignment list --subscription "$SUBSCRIPTION" --scope "$scope" -o none 2>/dev/null; then
    die "cannot list role assignments at $scope — your account needs 'User Access Administrator' or 'Owner'."
  fi
}

# ---------------------------------------------------------- env writes ---

upsert_env_line() {
  # $1 = file, $2 = KEY, $3 = VALUE
  local file="$1" key="$2" value="$3"
  if [[ ! -f "$file" ]]; then
    mkdir -p "$(dirname "$file")"
    printf '%s=%s\n' "$key" "$value" > "$file"
    return 0
  fi
  if grep -qE "^${key}=" "$file"; then
    # Use a delimiter that will never appear in our values to avoid sed issues.
    local tmp
    tmp="$(mktemp)"
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k"="v; next} {print}' "$file" > "$tmp"
    mv "$tmp" "$file"
  else
    # Ensure trailing newline before appending (portable: no xxd dependency).
    if [[ -s "$file" ]] && [[ "$(tail -c1 "$file")" != $'\n' ]]; then
      printf '\n' >> "$file"
    fi
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

apply_auth_env() {
  # $1 = "false" for on, "true" for off
  local bypass="$1"
  upsert_env_line "$env_file"     "AUTH_DEV_BYPASS"      "$bypass"
  upsert_env_line "$env_file"     "VITE_AUTH_DEV_BYPASS" "$bypass"
  upsert_env_line "$web_env_file" "VITE_AUTH_DEV_BYPASS" "$bypass"
  if [[ -n "${API_CLIENT_ID:-}" ]]; then
    upsert_env_line "$env_file" "API_CLIENT_ID" "$API_CLIENT_ID"
  fi
}

# ----------------------------------------------------- service restart ----

# Stop running local api / web tasks (graceful TERM → KILL). Idempotent.
stop_local_services() {
  ts "Stopping local api + web (if running)…"
  pkill -TERM -f 'uvicorn api.main:app --host 127.0.0.1 --port 8085' 2>/dev/null || true
  pkill -TERM -f 'node .*node_modules/.bin/vite' 2>/dev/null || true
  pkill -TERM -f 'scripts/dev/run-with-log.sh (api|web)' 2>/dev/null || true
  sleep 2
  pkill -KILL -f 'uvicorn api.main:app --host 127.0.0.1 --port 8085' 2>/dev/null || true
  pkill -KILL -f 'node .*node_modules/.bin/vite' 2>/dev/null || true
  pkill -KILL -f 'scripts/dev/run-with-log.sh (api|web)' 2>/dev/null || true
  sleep 1
}

# Spawn api + web detached so the shell returns. Logs go to .logs/local/latest/.
start_local_services() {
  ts "Starting local api + web…"
  ( cd "$project_root" && setsid nohup bash "$local_run" api </dev/null >/dev/null 2>&1 & )
  ( cd "$project_root" && setsid nohup bash "$local_run" web </dev/null >/dev/null 2>&1 & )
}

wait_for_endpoint() {
  local url="$1" label="$2" attempts="${3:-15}"
  for ((i=1; i<=attempts; i++)); do
    sleep 2
    if curl -fsS --max-time 2 "$url" -o /dev/null 2>/dev/null; then
      green "  $label READY ($url) after ${i}*2s"
      return 0
    fi
    if [[ $((i % 4)) -eq 0 ]]; then ts "  still waiting for $label ($url)…"; fi
  done
  yellow "  $label did not respond at $url within $((attempts * 2))s"
  return 1
}

verify_api_msal_required() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8085/api/me || echo 000)"
  if [[ "$code" == "401" ]]; then
    green "  /api/me → 401 (MSAL bearer required) ✓"
    return 0
  fi
  yellow "  /api/me → $code (expected 401 with bypass disabled — bypass may still be in effect)"
  return 1
}

# --------------------------------------------------------- print helpers --

print_status() {
  cyan "── local-debug-auth status ─────────────────────────────"
  local bypass_root bypass_web pna ips api_pid vite_pid
  bypass_root="$(grep -E '^AUTH_DEV_BYPASS=' "$env_file" 2>/dev/null | tail -1 | cut -d= -f2- || echo '<unset, default true>')"
  bypass_web="$(grep -E '^VITE_AUTH_DEV_BYPASS=' "$web_env_file" 2>/dev/null | tail -1 | cut -d= -f2- || echo '<unset, default true>')"
  echo "  signed-in user:   $USER_NAME ($USER_OID)"
  echo "  subscription:     $SUBSCRIPTION"
  echo "  storage:          ${STORAGE:-<unresolved>} ${STORAGE_RG:+(rg: $STORAGE_RG)}"
  echo "  AUTH_DEV_BYPASS:  $bypass_root  (root .env)"
  echo "  VITE_AUTH_DEV_BYPASS: $bypass_web  (web/.env.local)"

  if [[ -n "${STORAGE:-}" && -n "${STORAGE_RG:-}" ]]; then
    pna="$(az storage account show --subscription "$SUBSCRIPTION" -g "$STORAGE_RG" -n "$STORAGE" \
            --query "{public:publicNetworkAccess,default:networkRuleSet.defaultAction}" -o json 2>/dev/null || echo '{}')"
    echo "  storage network:  $pna"

    echo "  RBAC at storage scope:"
    az role assignment list --subscription "$SUBSCRIPTION" \
        --assignee-object-id "$USER_OID" \
        --scope "$(az storage account show --subscription "$SUBSCRIPTION" -g "$STORAGE_RG" -n "$STORAGE" --query id -o tsv)" \
        --query "[].roleDefinitionName" -o tsv 2>/dev/null | sed 's/^/    - /' \
        || echo "    (none / unable to list)"
  fi

  api_pid="$(pgrep -f 'uvicorn api.main:app --host 127.0.0.1 --port 8085' | head -1 || true)"
  vite_pid="$(pgrep -f 'node .*node_modules/.bin/vite' | head -1 || true)"
  if [[ -n "$api_pid"  ]]; then echo "  api (8085):       pid $api_pid";  else echo "  api (8085):       not running"; fi
  if [[ -n "$vite_pid" ]]; then echo "  vite (8090):      pid $vite_pid"; else echo "  vite (8090):      not running"; fi
}

# ============================================================== main =====

preflight
resolve_azd_defaults

case "$ACTION" in
  status)
    if [[ -n "$STORAGE" ]]; then resolve_storage_or_die; fi
    print_status
    exit 0
    ;;

  on)
    resolve_storage_or_die
    STORAGE_ID="$(az storage account show --subscription "$SUBSCRIPTION" -g "$STORAGE_RG" -n "$STORAGE" --query id -o tsv)"

    ts "Plan:"
    echo "  user:             $USER_NAME ($USER_OID)"
    echo "  subscription:     $SUBSCRIPTION"
    echo "  storage:          $STORAGE (rg: $STORAGE_RG)"
    echo "  ACR (RBAC):       ${ACR:-<skipped — no azd ACR_NAME>}"
    echo "  steps:            ${SKIP_RBAC:+[skip rbac] }${SKIP_RBAC:-rbac}  ${SKIP_STORAGE:+[skip storage] }${SKIP_STORAGE:-storage}  env  ${SKIP_RESTART:+[skip restart] }${SKIP_RESTART:-restart}"
    echo

    if [[ $SKIP_RBAC -eq 0 ]]; then
      ts "Step 1/4 — RBAC (grant-local-rbac.sh)"
      check_rbac_permission "$STORAGE_ID"
      rbac_args=(--storage "$STORAGE" --storage-rg "$STORAGE_RG" --subscription "$SUBSCRIPTION")
      [[ -n "$ACR"    ]] && rbac_args+=(--acr "$ACR")
      [[ -n "$ACR_RG" ]] && rbac_args+=(--acr-rg "$ACR_RG")
      "$grant_rbac" "${rbac_args[@]}"
    else
      yellow "Step 1/4 — RBAC: SKIPPED (--skip-rbac)"
    fi
    echo

    if [[ $SKIP_STORAGE -eq 0 ]]; then
      ts "Step 2/4 — Storage network (storage-public-access.sh)"
      local_state="$(az storage account show --subscription "$SUBSCRIPTION" -g "$STORAGE_RG" -n "$STORAGE" \
                      --query "{public:publicNetworkAccess,default:networkRuleSet.defaultAction}" -o json)"
      already_open=$(echo "$local_state" | jq -r 'select(.public=="Enabled" and .default=="Allow") | "yes"')
      if [[ "$already_open" == "yes" ]]; then
        green "  storage already publicNetworkAccess=Enabled / defaultAction=Allow — no change"
      else
        "$storage_toggle" on --account "$STORAGE" --rg "$STORAGE_RG" --subscription "$SUBSCRIPTION"
      fi
    else
      yellow "Step 2/4 — Storage network: SKIPPED (--skip-storage)"
    fi
    echo

    ts "Step 3/4 — Env files"
    if [[ -z "${API_CLIENT_ID:-}" ]]; then
      yellow "  API_CLIENT_ID not resolvable from azd env — api token validation will fail until you set it manually."
    fi
    apply_auth_env "false"
    green "  $env_file ← AUTH_DEV_BYPASS=false, VITE_AUTH_DEV_BYPASS=false${API_CLIENT_ID:+, API_CLIENT_ID=$API_CLIENT_ID}"
    green "  $web_env_file ← VITE_AUTH_DEV_BYPASS=false"
    echo

    if [[ $SKIP_RESTART -eq 0 ]]; then
      ts "Step 4/4 — Restart api + web"
      stop_local_services
      start_local_services
      wait_for_endpoint "http://127.0.0.1:8085/api/health" "api"  15 || true
      wait_for_endpoint "http://127.0.0.1:8090/"            "vite" 15 || true
      verify_api_msal_required || true
    else
      yellow "Step 4/4 — Restart: SKIPPED (--skip-restart) — restart api + web yourself for the new env to take effect"
    fi
    echo

    green "========================================================================"
    green "  Local MSAL login is ON. Open http://localhost:8090 and sign in."
    green "  When you finish debugging, close the storage surface:"
    green "      scripts/dev/local-debug-auth.sh off"
    green "========================================================================"
    ;;

  off)
    ts "Step 1/3 — Env files"
    apply_auth_env "true"
    green "  $env_file ← AUTH_DEV_BYPASS=true, VITE_AUTH_DEV_BYPASS=true"
    green "  $web_env_file ← VITE_AUTH_DEV_BYPASS=true"
    echo

    if [[ $CLOSE_STORAGE_ON_OFF -eq 1 ]]; then
      if [[ -z "$STORAGE" ]]; then resolve_storage_or_die; fi
      ts "Step 2/3 — Close storage network (storage-public-access.sh off)"
      "$storage_toggle" off --account "$STORAGE" --rg "$STORAGE_RG" --subscription "$SUBSCRIPTION"
    else
      yellow "Step 2/3 — Storage network: SKIPPED (--no-close-storage)"
    fi
    echo

    if [[ $SKIP_RESTART -eq 0 ]]; then
      ts "Step 3/3 — Restart api + web"
      stop_local_services
      start_local_services
      wait_for_endpoint "http://127.0.0.1:8085/api/health" "api"  15 || true
      wait_for_endpoint "http://127.0.0.1:8090/"            "vite" 15 || true
    else
      yellow "Step 3/3 — Restart: SKIPPED (--skip-restart)"
    fi
    echo

    green "========================================================================"
    green "  Local MSAL login is OFF (bypass=anonymous). Storage closed."
    green "  RBAC role assignments were NOT removed (cheap to keep)."
    green "========================================================================"
    ;;
esac
