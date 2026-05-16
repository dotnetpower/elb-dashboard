#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: scripts/dev/compose-with-log.sh <full|local> [docker compose args...]

Runs docker compose while writing project-local logs under .logs/local/latest/.
Foreground compose output is mirrored to compose-<profile>.log. If `up -d` is
used, a background `docker compose logs -f --no-color` follower is started and
mirrored to compose-<profile>-containers.log.

Examples:
  scripts/dev/compose-with-log.sh full up --build
  scripts/dev/compose-with-log.sh full up -d --build
  scripts/dev/compose-with-log.sh full logs --tail=50 api
  scripts/dev/compose-with-log.sh local up --build

Environment:
  COMPOSE_LOG_TAIL            Lines to replay for detached log follower (default: 200)
USAGE
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

profile=$1
shift

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd -- "$script_dir/../.." && pwd)
run_with_log="$script_dir/run-with-log.sh"
log_base=${LOCAL_LOG_BASE:-"$project_root/.logs/local"}
compose_log_tail=${COMPOSE_LOG_TAIL:-200}
mkdir -p "$log_base"

if ! [[ $compose_log_tail =~ ^[0-9]+$ ]]; then
  echo "ERROR: COMPOSE_LOG_TAIL must be an integer" >&2
  exit 2
fi

case "$profile" in
  full)
    compose_file="$project_root/scripts/dev/docker-compose.full.yml"
    project_name="elb-control-local"
    ;;
  local)
    compose_file="$project_root/scripts/dev/docker-compose.local.yml"
    project_name="elb-control-local-lite"
    ;;
  *)
    echo "ERROR: unknown compose profile: $profile" >&2
    usage
    exit 2
    ;;
esac

if [[ $# -eq 0 ]]; then
  set -- up --build
fi

service_name="compose-$profile"
container_log_service="compose-$profile-containers"
pid_file="$log_base/.$container_log_service.pid"

is_detached_up=false
if [[ ${1:-} == "up" ]]; then
  for arg in "$@"; do
    case "$arg" in
      -d|--detach)
        is_detached_up=true
        ;;
    esac
  done
fi

stop_existing_follower() {
  local old_pid pid pgid args
  if [[ ! -f $pid_file ]]; then
    old_pid=""
  else
    old_pid=$(cat "$pid_file" 2>/dev/null || true)
  fi

  if [[ -n ${old_pid:-} && $old_pid =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    kill -TERM -- "-$old_pid" 2>/dev/null || kill "$old_pid" 2>/dev/null || true
  fi

  while read -r pid pgid args; do
    [[ -n ${pid:-} && -n ${pgid:-} ]] || continue
    [[ $args == *"$project_name"* ]] || continue
    [[ $args == *"$compose_file"* ]] || continue
    [[ $args == *"logs -f"* || $args == *"logs --follow"* ]] || continue
    kill -TERM -- "-$pgid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  done < <(ps -eo pid=,pgid=,args=)

  # Give docker-compose-log children a moment to exit, then force remaining
  # exact matches. This path is only for local dev follower cleanup.
  sleep 0.1
  while read -r pid pgid args; do
    [[ -n ${pid:-} && -n ${pgid:-} ]] || continue
    [[ $args == *"$project_name"* ]] || continue
    [[ $args == *"$compose_file"* ]] || continue
    [[ $args == *"logs -f"* || $args == *"logs --follow"* ]] || continue
    kill -KILL -- "-$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  done < <(ps -eo pid=,pgid=,args=)

  rm -f "$pid_file"
}

is_stop_command=false
case ${1:-} in
  down|stop|rm)
    is_stop_command=true
    ;;
esac

if $is_stop_command; then
  stop_existing_follower
fi

start_detached_log_follower() {
  stop_existing_follower
  local latest_dir target_log
  latest_dir=$(readlink -f "$log_base/latest" 2>/dev/null || true)
  target_log="$latest_dir/$container_log_service.log"
  local command=(
    "$run_with_log"
    "$container_log_service"
    --
    docker
    compose
    -p
    "$project_name"
    -f
    "$compose_file"
    logs
    -f
    --no-color
    --tail
    "$compose_log_tail"
  )

  if command -v setsid >/dev/null 2>&1; then
    setsid "${command[@]}" </dev/null >/dev/null 2>&1 &
  else
    nohup "${command[@]}" </dev/null >/dev/null 2>&1 &
  fi
  printf '%s\n' "$!" > "$pid_file"
  if [[ -n $latest_dir && -d $latest_dir ]]; then
    for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      [[ -s $target_log ]] && break
      sleep 0.05
    done
  fi
  echo "compose $profile detached log follower pid=$! -> .logs/local/latest/$container_log_service.log"
}

cd "$project_root"
"$run_with_log" "$service_name" -- docker compose -p "$project_name" -f "$compose_file" "$@"

if $is_detached_up; then
  start_detached_log_follower
fi
