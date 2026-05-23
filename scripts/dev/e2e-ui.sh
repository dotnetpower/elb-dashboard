#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: scripts/dev/e2e-ui.sh <bypass|login|off|status> [options] [-- scenario-command ...]

Start a local UI E2E session in either dev-bypass mode or real MSAL login mode.
If a scenario command is supplied after --, this script exports the browser/auth
environment and runs that command. Without a scenario command, headed sessions
open the local UI and headless sessions stop after readiness checks.

Actions:
  bypass               Start api + web with AUTH_DEV_BYPASS=true. No Azure login required.
  login                Enable local MSAL login via local-run.sh auth-on, then start/verify UI.
  off                  Revert local MSAL mode via local-run.sh auth-off.
  status               Print local auth/server status.

Browser options:
  --headed             Prefer a visible browser.
  --headless           Prefer headless execution.
  --ask-browser        Ask briefly; any Enter response chooses headed, timeout chooses headless.
  --prompt-timeout N   Seconds to wait for --ask-browser. Default: 5.
  --no-open            Do not xdg-open the local UI in headed mode without a scenario command.
  --open               Open the local UI in headed mode without a scenario command. Default.

Session options:
  --url URL            SPA URL. Default: http://localhost:8090
  --api-url URL        API URL. Default: http://127.0.0.1:8085
  --skip-restart       Do not restart local api + web after changing bypass env.
  --auth-arg VALUE     Extra argument passed to local-run.sh auth-on/auth-off.
                       Repeat for multiple values, e.g. --auth-arg --skip-rbac.
  -h | --help          Show this help.

Examples:
  scripts/dev/e2e-ui.sh bypass --headless
  scripts/dev/e2e-ui.sh bypass --headed
  scripts/dev/e2e-ui.sh login --ask-browser
  scripts/dev/e2e-ui.sh bypass --headless -- npm run test:e2e

Exported for scenario commands:
  E2E_BASE_URL, E2E_API_URL, E2E_AUTH_MODE, E2E_BROWSER_MODE,
  HEADLESS, PLAYWRIGHT_HEADLESS, PWDEBUG
USAGE
}

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
cyan() { printf '\033[36m%s\033[0m\n' "$*"; }
ts() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { red "ERROR: $*" >&2; exit 1; }

[[ $# -ge 1 ]] || { usage; exit 2; }

ACTION="$1"
shift || true
case "$ACTION" in
  bypass|login|off|status) ;;
  -h|--help|help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/../.." && pwd)"
local_run="$script_dir/local-run.sh"
env_file="$project_root/.env"
web_env_file="$project_root/web/.env.local"

[[ -x "$local_run" ]] || die "missing helper: $local_run"

browser_choice="${E2E_BROWSER_MODE:-auto}"
prompt_timeout="${E2E_BROWSER_PROMPT_TIMEOUT:-5}"
base_url="${E2E_BASE_URL:-http://localhost:8090}"
api_url="${E2E_API_URL:-http://127.0.0.1:8085}"
open_visible=1
skip_restart=0
auth_args=()
scenario=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --headed)
      browser_choice="headed"
      shift
      ;;
    --headless)
      browser_choice="headless"
      shift
      ;;
    --ask-browser)
      browser_choice="ask"
      shift
      ;;
    --prompt-timeout)
      prompt_timeout="${2:-}"
      [[ "$prompt_timeout" =~ ^[0-9]+$ ]] || die "--prompt-timeout must be a non-negative integer"
      shift 2
      ;;
    --url)
      base_url="${2:-}"
      [[ -n "$base_url" ]] || die "--url requires a value"
      shift 2
      ;;
    --api-url)
      api_url="${2:-}"
      [[ -n "$api_url" ]] || die "--api-url requires a value"
      shift 2
      ;;
    --open)
      open_visible=1
      shift
      ;;
    --no-open)
      open_visible=0
      shift
      ;;
    --skip-restart)
      skip_restart=1
      shift
      ;;
    --auth-arg)
      [[ -n "${2:-}" ]] || die "--auth-arg requires a value"
      auth_args+=("$2")
      shift 2
      ;;
    --)
      shift
      scenario=("$@")
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

upsert_env_line() {
  local file="$1" key="$2" value="$3"
  if [[ ! -f "$file" ]]; then
    mkdir -p "$(dirname -- "$file")"
    printf '%s=%s\n' "$key" "$value" > "$file"
    return 0
  fi

  local tmp
  tmp="$(mktemp)"
  if awk -v k="$key" -v v="$value" '
    BEGIN { FS = OFS = "="; found = 0 }
    $1 == k { print k"="v; found = 1; next }
    { print }
    END { if (!found) print k"="v }
  ' "$file" > "$tmp"; then
    mv "$tmp" "$file"
  else
    rm -f "$tmp"
    return 1
  fi
}

apply_bypass_env() {
  upsert_env_line "$env_file" "AUTH_DEV_BYPASS" "true"
  upsert_env_line "$env_file" "VITE_AUTH_DEV_BYPASS" "true"
  upsert_env_line "$web_env_file" "VITE_AUTH_DEV_BYPASS" "true"
}

stop_local_services() {
  ts "Stopping local api + web if they are running..."
  pkill -TERM -f 'uvicorn api.main:app --host 127.0.0.1 --port 8085' 2>/dev/null || true
  pkill -TERM -f 'node .*node_modules/.bin/vite' 2>/dev/null || true
  pkill -TERM -f 'scripts/dev/run-with-log.sh (api|web)' 2>/dev/null || true
  sleep 2
  pkill -KILL -f 'uvicorn api.main:app --host 127.0.0.1 --port 8085' 2>/dev/null || true
  pkill -KILL -f 'node .*node_modules/.bin/vite' 2>/dev/null || true
  pkill -KILL -f 'scripts/dev/run-with-log.sh (api|web)' 2>/dev/null || true
}

start_local_services() {
  ts "Starting local api + web..."
  (
    cd "$project_root"
    setsid nohup env ELB_SKIP_AZURE_CONTEXT_CHECK=true bash "$local_run" api </dev/null >/dev/null 2>&1 &
  )
  (
    cd "$project_root"
    setsid nohup env ELB_SKIP_AZURE_CONTEXT_CHECK=true bash "$local_run" web </dev/null >/dev/null 2>&1 &
  )
}

wait_for_endpoint() {
  local url="$1" label="$2" attempts="${3:-20}"
  local index
  for ((index = 1; index <= attempts; index++)); do
    if curl -fsS --max-time 2 "$url" -o /dev/null 2>/dev/null; then
      green "  $label ready: $url"
      return 0
    fi
    sleep 1
  done
  yellow "  $label did not respond at $url within ${attempts}s"
  return 1
}

require_endpoint() {
  local url="$1" label="$2" attempts="${3:-30}"
  if wait_for_endpoint "$url" "$label" "$attempts"; then
    return 0
  fi
  die "$label is not ready at $url. Check .logs/local/latest/${label}.log."
}

require_e2e_targets() {
  require_endpoint "$api_url/api/health" "api" 35
  require_endpoint "$base_url/" "web" 45
}

resolve_browser_mode() {
  local choice="$1"
  case "$choice" in
    headed|headless)
      printf '%s' "$choice"
      return 0
      ;;
    auto)
      if [[ "${CI:-}" == "true" || ! -t 0 ]]; then
        printf 'headless'
      else
        prompt_browser_mode
      fi
      ;;
    ask)
      prompt_browser_mode
      ;;
    *)
      die "invalid browser mode: $choice"
      ;;
  esac
}

prompt_browser_mode() {
  if [[ ! -t 0 ]]; then
    printf 'headless'
    return 0
  fi

  local reply=""
  printf 'Press Enter within %ss to open a visible browser; no response runs headless... ' "$prompt_timeout" >&2
  if IFS= read -r -t "$prompt_timeout" reply; then
    printf '\n' >&2
    printf 'headed'
  else
    printf '\n' >&2
    printf 'headless'
  fi
}

open_ui_if_requested() {
  [[ "$E2E_BROWSER_MODE" == "headed" ]] || return 0
  [[ "$open_visible" -eq 1 ]] || return 0
  [[ "${#scenario[@]}" -eq 0 ]] || return 0

  if command -v xdg-open >/dev/null 2>&1; then
    ts "Opening $base_url"
    xdg-open "$base_url" >/dev/null 2>&1 || yellow "  xdg-open could not open $base_url"
  else
    yellow "  xdg-open not found; open this URL manually: $base_url"
  fi
}

export_scenario_env() {
  export E2E_BASE_URL="$base_url"
  export E2E_API_URL="$api_url"
  export E2E_AUTH_MODE="$ACTION"
  export E2E_BROWSER_MODE
  if [[ "$E2E_BROWSER_MODE" == "headless" ]]; then
    export HEADLESS=1
    export PLAYWRIGHT_HEADLESS=1
    unset PWDEBUG || true
  else
    export HEADLESS=0
    export PLAYWRIGHT_HEADLESS=0
    export PWDEBUG="${PWDEBUG:-0}"
  fi
}

print_summary() {
  cyan "-- e2e-ui session ----------------------------------------"
  echo "  auth mode:     $ACTION"
  echo "  browser mode:  $E2E_BROWSER_MODE"
  echo "  ui url:        $base_url"
  echo "  api url:       $api_url"
  if [[ "${#scenario[@]}" -gt 0 ]]; then
    printf '  scenario:      '
    printf '%q ' "${scenario[@]}"
    printf '\n'
  else
    echo "  scenario:      <none>"
  fi
}

run_scenario_if_present() {
  [[ "${#scenario[@]}" -gt 0 ]] || return 0
  ts "Running scenario command..."
  (
    cd "$project_root"
    exec "${scenario[@]}"
  )
}

case "$ACTION" in
  bypass)
    E2E_BROWSER_MODE="$(resolve_browser_mode "$browser_choice")"
    export_scenario_env
    ts "Preparing local dev-bypass UI session..."
    apply_bypass_env
    green "  AUTH_DEV_BYPASS=true written to local env files"
    if [[ "$skip_restart" -eq 0 ]]; then
      stop_local_services
      start_local_services
      require_e2e_targets
    else
      yellow "  restart skipped; existing services must already match the selected env"
      if [[ "${#scenario[@]}" -gt 0 ]]; then
        require_e2e_targets
      fi
    fi
    print_summary
    open_ui_if_requested
    run_scenario_if_present
    ;;

  login)
    E2E_BROWSER_MODE="$(resolve_browser_mode "$browser_choice")"
    export_scenario_env
    ts "Preparing local MSAL login UI session..."
    "$local_run" auth-on "${auth_args[@]}"
    require_e2e_targets
    print_summary
    open_ui_if_requested
    run_scenario_if_present
    ;;

  off)
    ts "Reverting local MSAL login session..."
    "$local_run" auth-off "${auth_args[@]}"
    ;;

  status)
    "$local_run" auth-status "${auth_args[@]}"
    ;;
esac