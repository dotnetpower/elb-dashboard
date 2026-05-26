#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: scripts/dev/local-run.sh <start|stop|restart|status|api|worker|beat|web|redis|terminal-exec|smoke|storage-on|storage-off|storage-status|auth-on|auth-off|auth-status|compose-full|compose-local> [-- extra args]

Starts one local development process through run-with-log.sh so direct terminal
runs and VS Code tasks both write to .logs/local/latest/.

Examples:
  scripts/dev/local-run.sh start           # launch host-mode servers in the background and return immediately
  scripts/dev/local-run.sh stop            # stop host-mode servers, compose stacks, and local Redis
  scripts/dev/local-run.sh restart         # stop, then launch host-mode servers in the background
  scripts/dev/local-run.sh status          # inspect local server ports/processes
  scripts/dev/local-run.sh api
  scripts/dev/local-run.sh web
  scripts/dev/local-run.sh worker
  scripts/dev/local-run.sh terminal-exec   # exec_server.py on 127.0.0.1:7682 so api/worker can run kubectl/az locally
  scripts/dev/local-run.sh storage-on      # open workload Storage to this caller IP for local debugging
  scripts/dev/local-run.sh storage-off     # restore workload Storage to publicNetworkAccess=Disabled
  scripts/dev/local-run.sh auth-on         # one-shot: RBAC + storage-on + AUTH_DEV_BYPASS=false + restart api/web
  scripts/dev/local-run.sh auth-off        # revert: bypass=true + storage-off + restart api/web
  scripts/dev/local-run.sh auth-status     # show current local MSAL/Storage/RBAC state
  scripts/dev/local-run.sh smoke -- --url http://127.0.0.1:8085
  scripts/dev/local-run.sh compose-full -- up -d --build

Environment defaults can be overridden before invoking the script.
USAGE
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

service=$1
shift
if [[ ${1:-} == "--" ]]; then
  shift
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd -- "$script_dir/../.." && pwd)
run_with_log="$script_dir/run-with-log.sh"
compose_with_log="$script_dir/compose-with-log.sh"

if [[ "${ELB_LOCAL_RUN_LOGGED:-}" != "1" ]]; then
  case "$service" in
    api|worker|beat|web|terminal-exec)
      export ELB_LOCAL_RUN_LOGGED=1
      exec "$run_with_log" "$service" -- "$script_dir/local-run.sh" "$service" -- "$@"
      ;;
  esac
fi

exec_local_service() {
  local service_name=$1
  shift
  if [[ "${ELB_LOCAL_RUN_LOGGED:-}" == "1" ]]; then
    exec "$@"
  fi
  exec "$run_with_log" "$service_name" -- "$@"
}

load_local_azure_env() {
  local env_file="$project_root/.env"
  [[ -f "$env_file" ]] || return 0

  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key#export }"
    case "$key" in
      AZURE_SUBSCRIPTION_ID|AZURE_TENANT_ID|ELB_LOCAL_STORAGE_ACCOUNT|ELB_LOCAL_STORAGE_RG|API_CLIENT_ID|AUTH_DEV_BYPASS|VITE_AUTH_DEV_BYPASS)
        if [[ -z "${!key:-}" ]]; then
          if [[ "$value" == \"*\" && "$value" == *\" ]]; then
            value="${value:1:${#value}-2}"
          elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
            value="${value:1:${#value}-2}"
          fi
          export "$key=$value"
        fi
        ;;
    esac
  done < "$env_file"
}

azd_env_value() {
  local name=$1
  command -v azd >/dev/null 2>&1 || return 1
  azd env get-values 2>/dev/null | awk -F= -v key="$name" '
    $1 == key {
      gsub(/"/, "", $2)
      print $2
      exit
    }
  '
}

validate_azure_cli_context() {
  case "${ELB_SKIP_AZURE_CONTEXT_CHECK:-}" in
    1|true|TRUE|yes|YES)
      return 0
      ;;
  esac

  [[ -n "${AZURE_SUBSCRIPTION_ID:-}" || -n "${AZURE_TENANT_ID:-}" ]] || return 0
  if ! command -v az >/dev/null 2>&1; then
    echo "WARNING: az CLI not found; skipping Azure context validation." >&2
    return 0
  fi

  local account current_subscription current_tenant
  if ! account=$(az account show --query '[id,tenantId]' -o tsv 2>/dev/null); then
    echo "ERROR: az CLI is not signed in. Run: az login --tenant \"${AZURE_TENANT_ID:-<tenant-id>}\"" >&2
    exit 1
  fi
  current_subscription=$(printf '%s\n' "$account" | sed -n '1p')
  current_tenant=$(printf '%s\n' "$account" | sed -n '2p')

  if [[ -n "${AZURE_SUBSCRIPTION_ID:-}" && "$current_subscription" != "$AZURE_SUBSCRIPTION_ID" ]]; then
    echo "ERROR: az CLI default subscription is $current_subscription, expected $AZURE_SUBSCRIPTION_ID." >&2
    echo "Run: az account set --subscription \"$AZURE_SUBSCRIPTION_ID\"" >&2
    exit 1
  fi
  if [[ -n "${AZURE_TENANT_ID:-}" && "$current_tenant" != "$AZURE_TENANT_ID" ]]; then
    echo "ERROR: az CLI tenant is $current_tenant, expected $AZURE_TENANT_ID." >&2
    echo "Run: az login --tenant \"$AZURE_TENANT_ID\" && az account set --subscription \"${AZURE_SUBSCRIPTION_ID:-<subscription-id>}\"" >&2
    exit 1
  fi
}

with_common_env() {
  load_local_azure_env
  validate_azure_cli_context
  export PYTHONPATH="$project_root${PYTHONPATH:+:$PYTHONPATH}"
  export LOG_LEVEL=${LOG_LEVEL:-INFO}
}

with_celery_env() {
  export CELERY_BROKER_URL=${CELERY_BROKER_URL:-redis://127.0.0.1:6379/0}
  export CELERY_RESULT_BACKEND=${CELERY_RESULT_BACKEND:-redis://127.0.0.1:6379/1}
  export OPS_REDIS_URL=${OPS_REDIS_URL:-redis://127.0.0.1:6379/2}
}

with_local_storage_env() {
  local storage_account=${ELB_LOCAL_STORAGE_ACCOUNT:-}
  if [[ -z "$storage_account" ]]; then
    storage_account=$(azd_env_value STORAGE_ACCOUNT_NAME || true)
  fi
  storage_account=${storage_account:-elbstg01}
  export AZURE_TABLE_ENDPOINT=${AZURE_TABLE_ENDPOINT:-https://${storage_account}.table.core.windows.net}
  export AZURE_BLOB_ENDPOINT=${AZURE_BLOB_ENDPOINT:-https://${storage_account}.blob.core.windows.net}
  export LOCAL_DEBUG_AUTO_OPEN_STORAGE=${LOCAL_DEBUG_AUTO_OPEN_STORAGE:-true}
}

run_storage_public_access() {
  local action=$1
  shift || true
  local storage_account=${ELB_LOCAL_STORAGE_ACCOUNT:-}
  local storage_rg=${ELB_LOCAL_STORAGE_RG:-}
  if [[ -z "$storage_account" ]]; then
    storage_account=$(azd_env_value STORAGE_ACCOUNT_NAME || true)
  fi
  if [[ -z "$storage_rg" ]]; then
    storage_rg=$(azd_env_value AZURE_RESOURCE_GROUP || true)
  fi
  storage_account=${storage_account:-elbstg01}
  storage_rg=${storage_rg:-rg-elb-01}
  exec "$script_dir/storage-public-access.sh" "$action" \
    --account "$storage_account" \
    --rg "$storage_rg" \
    "$@"
}

# Default token + upstream so `local-run.sh api` and `local-run.sh worker`
# can talk to a host-side `local-run.sh terminal-exec` without extra setup.
# The token is intentionally non-secret — the exec_server only binds
# 127.0.0.1 so only processes on the same host can reach it. Override
# EXEC_TOKEN if you want to mirror a deployed Container App's secret.
with_terminal_exec_env() {
  export EXEC_TOKEN=${EXEC_TOKEN:-dev-exec-token-not-secret-but-long-enough-for-startup-check}
  export TERMINAL_EXEC_UPSTREAM=${TERMINAL_EXEC_UPSTREAM:-http://127.0.0.1:7682}
}

api_probe_host() {
  local host=$1
  case "$host" in
    0.0.0.0|::)
      printf '127.0.0.1'
      ;;
    *)
      printf '%s' "$host"
      ;;
  esac
}

api_port_is_listening() {
  local port=$1
  ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .
}

api_health_ready() {
  local base_url=$1
  command -v curl >/dev/null 2>&1 \
    && curl -fsS --max-time 1 "$base_url/api/health" >/dev/null 2>&1
}

http_health_ready() {
  local url=$1
  command -v curl >/dev/null 2>&1 \
    && curl -fsS --max-time 1 "$url" >/dev/null 2>&1
}

wait_for_api_health() {
  local base_url=$1
  for _attempt in 1 2 3 4 5 6 7 8 9 10; do
    api_health_ready "$base_url" && return 0
    sleep 0.5
  done
  return 1
}

describe_api_port_owner() {
  local port=$1
  echo "ERROR: 127.0.0.1:$port is already in use, but it is not the local API health endpoint." >&2
  echo "Stop the process below, or choose a matching API_PORT/VITE_API_BASE_URL pair." >&2
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :$port" >&2 || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2 || true
  else
    echo "Install 'ss' or 'lsof' to inspect the listener." >&2
  fi
}

describe_port_owner() {
  local port=$1
  local service_name=$2
  echo "ERROR: 127.0.0.1:$port is already in use, but it is not the expected $service_name endpoint." >&2
  echo "Stop the process below, or choose a different port for $service_name." >&2
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :$port" >&2 || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2 || true
  else
    echo "Install 'ss' or 'lsof' to inspect the listener." >&2
  fi
}

new_local_log_session() {
  local log_base=${LOCAL_LOG_BASE:-"$project_root/.logs/local"}
  local session now
  now=$(date -u +%s)
  session=${LOCAL_LOG_SESSION:-$(date -u +%Y%m%dT%H%M%SZ)-server}
  mkdir -p "$log_base/$session"
  printf '%s %s\n' "$session" "$now" > "$log_base/.current-session"
  ln -sfn "$session" "$log_base/latest"
  printf '%s\n' "$session"
}

service_is_running() {
  local service_name=$1
  local api_port=${API_PORT:-8085}
  local web_port=${WEB_PORT:-8090}
  local exec_port=${EXEC_PORT:-7682}

  case "$service_name" in
    redis)
      docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^elb-dev-redis$'
      ;;
    api)
      api_health_ready "http://127.0.0.1:$api_port"
      ;;
    web)
      http_health_ready "http://127.0.0.1:$web_port/"
      ;;
    terminal-exec)
      http_health_ready "http://127.0.0.1:$exec_port/healthz"
      ;;
    worker)
      pgrep -f 'api/run_celery_workers\.py|python3 -m celery -A api\.celery_app:celery_app worker|celery -A api\.celery_app:celery_app worker' >/dev/null 2>&1
      ;;
    beat)
      pgrep -f 'celery -A api\.celery_app beat' >/dev/null 2>&1
      ;;
    *)
      return 1
      ;;
  esac
}

start_detached_service() {
  local service_name=$1
  local session=$2
  local log_base=${LOCAL_LOG_BASE:-"$project_root/.logs/local"}
  local session_dir="$log_base/$session"
  local launch_log="$session_dir/start.log"
  local launcher_pid_file="$session_dir/$service_name.launch.pid"
  local script_path="$script_dir/local-run.sh"

  if service_is_running "$service_name"; then
    printf '%-14s already running\n' "$service_name"
    return 0
  fi

  printf '%-14s launching in background\n' "$service_name"
  (
    cd "$project_root"
    nohup env \
      LOCAL_LOG_SESSION="$session" \
      LOCAL_LOG_CONSOLE=false \
      "$script_path" "$service_name" \
      >> "$launch_log" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$launcher_pid_file"
  )
}

terminate_matching_processes() {
  local pattern
  for pattern in "$@"; do
    pkill -TERM -f "$pattern" 2>/dev/null || true
  done
  for _attempt in 1 2 3 4 5 6 7 8 9 10; do
    local any_running=false
    for pattern in "$@"; do
      if pgrep -f "$pattern" >/dev/null 2>&1; then
        any_running=true
        break
      fi
    done
    [[ "$any_running" == false ]] && return 0
    sleep 0.5
  done
  for pattern in "$@"; do
    pkill -KILL -f "$pattern" 2>/dev/null || true
  done
}

run_server_start() {
  local session
  with_common_env
  session=$(new_local_log_session)
  export LOCAL_LOG_SESSION="$session"
  export LOCAL_LOG_CONSOLE=false

  run_redis
  for service_name in terminal-exec api worker beat web; do
    start_detached_service "$service_name" "$session"
  done

  echo "local servers are launching in the background"
  echo "logs: .logs/local/$session/"
  echo "api: http://127.0.0.1:${API_PORT:-8085}/api/health"
  echo "web: http://127.0.0.1:${WEB_PORT:-8090}/"
}

run_server_stop() {
  local api_port=${API_PORT:-8085}
  local web_port=${WEB_PORT:-8090}
  local exec_port=${EXEC_PORT:-7682}

  # Stop broker-dependent host processes (worker/beat/api/web/terminal-exec)
  # BEFORE removing Redis. If Redis goes first, Celery raises
  # "Connection closed by server" tracebacks during shutdown.
  terminate_matching_processes \
    "run-with-log\.sh worker -- .*/local-run\.sh worker" \
    'api/run_celery_workers\.py' \
    'python3 -m celery -A api\.celery_app:celery_app worker' \
    'celery -A api\.celery_app:celery_app worker' \
    "run-with-log\.sh beat -- .*/local-run\.sh beat" \
    'celery -A api\.celery_app beat' \
    "run-with-log\.sh api -- .*/local-run\.sh api" \
    "run-with-log\.sh api -- uv run uvicorn api\.main:app .* --port $api_port" \
    "uvicorn api\.main:app .* --port $api_port" \
    "run-with-log\.sh web -- .*/local-run\.sh web" \
    "run-with-log\.sh web -- npm run dev" \
    "vite .*--port $web_port" \
    "$project_root/web/node_modules/.bin/vite" \
    "run-with-log\.sh terminal-exec -- .*/local-run\.sh terminal-exec" \
    "run-with-log\.sh terminal-exec -- python3 terminal/exec_server\.py" \
    'terminal/exec_server\.py'

  # Now that host workers/api have detached from Redis, tear down the
  # in-revision broker, compose stacks, and Azurite.
  "$compose_with_log" full down --remove-orphans >/dev/null 2>&1 || true
  "$compose_with_log" local down --remove-orphans >/dev/null 2>&1 || true
  docker rm -f elb-dev-redis azurite-elb >/dev/null 2>&1 || true

  echo "local servers stopped"
  echo "checked ports: api=$api_port web=$web_port exec=$exec_port redis=6379 compose=18080"
}

run_server_status() {
  local service_name
  for service_name in redis terminal-exec api worker beat web; do
    if service_is_running "$service_name"; then
      printf '%-14s running\n' "$service_name"
    else
      printf '%-14s stopped\n' "$service_name"
    fi
  done
}

run_api() {
  with_common_env
  with_celery_env
  with_terminal_exec_env
  with_local_storage_env
  export AUTH_DEV_BYPASS=${AUTH_DEV_BYPASS:-true}
  export ENABLE_DOCS=${ENABLE_DOCS:-true}
  export CORS_ALLOW_ORIGINS=${CORS_ALLOW_ORIGINS:-http://localhost:8090,http://127.0.0.1:8090}

  local api_host=${API_HOST:-127.0.0.1}
  local api_port=${API_PORT:-8085}
  local probe_host
  probe_host=$(api_probe_host "$api_host")
  local api_base_url="http://$probe_host:$api_port"
  local api_lock_file="$project_root/.logs/local/api-$api_port.lock"

  mkdir -p "$(dirname -- "$api_lock_file")"
  if command -v flock >/dev/null 2>&1; then
    exec {api_lock_fd}>"$api_lock_file"
    if ! flock -n "$api_lock_fd"; then
      if api_health_ready "$api_base_url"; then
        echo "api already running at $api_base_url (health OK)." >&2
        exit 0
      fi
      echo "api start is already in progress for $api_base_url; waiting for health..." >&2
      if wait_for_api_health "$api_base_url"; then
        echo "api already running at $api_base_url (health OK)." >&2
        exit 0
      fi
      echo "ERROR: another api start holds $api_lock_file, but health did not become ready." >&2
      exit 1
    fi
  else
    echo "WARNING: flock not found; concurrent api starts may still race on port $api_port." >&2
  fi

  if api_port_is_listening "$api_port"; then
    if api_health_ready "$api_base_url"; then
      echo "api already running at $api_base_url (health OK)." >&2
      echo "Stop the existing api task/process first if you need a fresh api process." >&2
      exit 0
    fi
    describe_api_port_owner "$api_port"
    exit 1
  fi

  cd "$project_root/api"
  exec_local_service api uv run uvicorn api.main:app --host "$api_host" --port "$api_port" "$@"
}

run_redis() {
  "$run_with_log" redis -- bash -lc '
set -euo pipefail
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker CLI not found. Install Docker or start Docker Desktop." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon not reachable. Start Docker (e.g. systemctl --user start docker-desktop)." >&2
  exit 1
fi
if docker ps --format "{{.Names}}" | grep -q "^elb-dev-redis$"; then
  echo "redis already running"
elif docker ps -a --format "{{.Names}}" | grep -q "^elb-dev-redis$"; then
  docker start elb-dev-redis >/dev/null
  echo "redis started"
else
  if ss -ltn 2>/dev/null | grep -q ":6379 "; then
    echo "ERROR: 127.0.0.1:6379 is in use by another process. Stop it or remove the conflicting container." >&2
    exit 1
  fi
  docker run -d --name elb-dev-redis -p 127.0.0.1:6379:6379 redis:7-alpine >/dev/null
  echo "redis created"
fi
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  docker exec elb-dev-redis redis-cli ping >/dev/null 2>&1 && echo "redis ready" && exit 0
  sleep 0.5
done
echo "ERROR: redis failed to become ready in 5s" >&2
docker logs --tail=50 elb-dev-redis >&2 || true
exit 1
'
}

case "$service" in
  start)
    run_server_start "$@"
    ;;
  stop)
    run_server_stop "$@"
    ;;
  restart)
    run_server_stop "$@"
    run_server_start "$@"
    ;;
  status)
    run_server_status "$@"
    ;;
  api)
    run_api "$@"
    ;;
  worker)
    with_common_env
    with_celery_env
    with_terminal_exec_env
    with_local_storage_env
    cd "$project_root"
    # Reap any orphaned celery worker processes from a previous run before
    # spawning fresh ones. Without this the Redis broker accumulates multiple
    # workers (each from a separate `worker: start`) and submit tasks land
    # randomly on stale instances that still hold the pre-fix module code,
    # silently re-introducing already-fixed bugs (see
    # docs/features_change/2026-05/2026-05-22-blast-submit-pipeline-hardening.md).
    pkill -TERM -f 'run-with-log\.sh worker -- uv run python api/run_celery_workers\.py' 2>/dev/null || true
    pkill -TERM -f 'api/run_celery_workers\.py' 2>/dev/null || true
    for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      if ! pgrep -f 'api/run_celery_workers\.py|python3 -m celery -A api\.celery_app:celery_app worker' >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    pkill -KILL -f 'python3 -m celery -A api\.celery_app:celery_app worker' 2>/dev/null || true
    pkill -KILL -f 'api/run_celery_workers\.py' 2>/dev/null || true
    exec_local_service worker uv run python api/run_celery_workers.py "$@"
    ;;
  beat)
    with_common_env
    with_celery_env
    with_local_storage_env
    cd "$project_root"
    # Reap orphaned beat schedulers for the same reason as workers above. A
    # leftover beat would double-fire periodic tasks (reconcile_stale_jobs,
    # reconcile_auto_warmup, …) against the broker.
    pkill -TERM -f 'celery -A api\.celery_app beat' 2>/dev/null || true
    sleep 1
    pkill -KILL -f 'celery -A api\.celery_app beat' 2>/dev/null || true
    exec_local_service beat uv run celery -A api.celery_app beat -l info --schedule=/tmp/elb-celerybeat-schedule --pidfile=/tmp/elb-celerybeat.pid "$@"
    ;;
  web)
    with_common_env
    export VITE_API_BASE_URL=${VITE_API_BASE_URL:-http://localhost:8085}
    export VITE_AUTH_DEV_BYPASS=${VITE_AUTH_DEV_BYPASS:-true}
    export VITE_AZURE_TENANT_ID=${VITE_AZURE_TENANT_ID:-${AZURE_TENANT_ID:-common}}
    # Auto-pull the App Registration clientId from azd env so a clone-and-run
    # after `azd up` does not need a manual web/.env.local edit. The placeholder
    # all-zero UUID shipped in web/.env.example is treated as "unset" here so it
    # cannot reach MSAL (AADSTS700038 otherwise).
    if [[ -z "${VITE_AZURE_CLIENT_ID:-}" || "${VITE_AZURE_CLIENT_ID:-}" == "00000000-0000-0000-0000-000000000000" ]]; then
      if command -v azd >/dev/null 2>&1; then
        azd_client_id="$(azd env get-values 2>/dev/null | awk -F= '/^API_CLIENT_ID=/{gsub(/"/, "", $2); print $2; exit}')"
        if [[ -n "${azd_client_id:-}" && "$azd_client_id" != "00000000-0000-0000-0000-000000000000" ]]; then
          export VITE_AZURE_CLIENT_ID="$azd_client_id"
          echo "[local-run] Picked up VITE_AZURE_CLIENT_ID from azd env (API_CLIENT_ID=$azd_client_id)." >&2
        fi
      fi
    fi
    web_port=${WEB_PORT:-8090}
    web_url="http://127.0.0.1:$web_port/"
    if api_port_is_listening "$web_port"; then
      if http_health_ready "$web_url"; then
        echo "web already running at $web_url." >&2
        echo "Stop the existing web task/process first if you need a fresh Vite process." >&2
        exit 0
      fi
      describe_port_owner "$web_port" "web"
      exit 1
    fi
    cd "$project_root/web"
    exec_local_service web npm run dev "$@"
    ;;
  redis)
    run_redis "$@"
    ;;
  storage-on)
    run_storage_public_access on "$@"
    ;;
  storage-off)
    run_storage_public_access off "$@"
    ;;
  storage-status)
    run_storage_public_access status "$@"
    ;;
  auth-on)
    exec "$script_dir/local-debug-auth.sh" on "$@"
    ;;
  auth-off)
    exec "$script_dir/local-debug-auth.sh" off "$@"
    ;;
  auth-status)
    exec "$script_dir/local-debug-auth.sh" status "$@"
    ;;
  terminal-exec)
    # Run terminal/exec_server.py on the host so `local-run.sh api`/`worker`
    # can drive kubectl/az/azcopy without the docker-compose terminal sidecar.
    # Requires az/kubectl/azcopy on PATH (the exec_server's allowlist).
    with_terminal_exec_env
    local_elb_root=${LOCAL_ELASTIC_BLAST_AZURE_ROOT:-$HOME/dev/elastic-blast-azure}
    if [[ -x "$local_elb_root/venv/bin/elastic-blast" ]]; then
      export PATH="$local_elb_root/venv/bin:$PATH"
      export PYTHONPATH="$project_root/terminal/runtime_overrides:$local_elb_root/src${PYTHONPATH:+:$PYTHONPATH}"
      export ELB_DASHBOARD_FAST_JSON_SUBMIT_CLEANUP=${ELB_DASHBOARD_FAST_JSON_SUBMIT_CLEANUP:-1}
      export ELB_DASHBOARD_FAST_AZURE_IO=${ELB_DASHBOARD_FAST_AZURE_IO:-1}
    fi
    export AZCOPY_AUTO_LOGIN_TYPE=${AZCOPY_AUTO_LOGIN_TYPE:-AZCLI}
    for bin in az kubectl azcopy; do
      if ! command -v "$bin" >/dev/null 2>&1; then
        echo "ERROR: '$bin' not found on PATH — install it before running terminal-exec." >&2
        exit 1
      fi
    done
    exec_port=${EXEC_PORT:-7682}
    exec_url="http://127.0.0.1:$exec_port/healthz"
    if api_port_is_listening "$exec_port"; then
      if http_health_ready "$exec_url"; then
        echo "terminal-exec already running at $exec_url." >&2
        echo "Stop the existing terminal-exec task/process first if you need a fresh exec server." >&2
        exit 0
      fi
      describe_port_owner "$exec_port" "terminal-exec"
      exit 1
    fi
    cd "$project_root"
    exec_local_service terminal-exec python3 terminal/exec_server.py "$@"
    ;;
  smoke)
    cd "$project_root"
    exec "$run_with_log" smoke -- uv run python scripts/dev/smoke_api.py --url http://127.0.0.1:8085 "$@"
    ;;
  compose-full)
    exec "$compose_with_log" full "$@"
    ;;
  compose-local)
    exec "$compose_with_log" local "$@"
    ;;
  *)
    echo "ERROR: unknown local service: $service" >&2
    usage
    exit 2
    ;;
esac
