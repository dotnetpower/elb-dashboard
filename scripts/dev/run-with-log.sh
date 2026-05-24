#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: scripts/dev/run-with-log.sh <service-name> -- <command> [args...]

Runs a local development command while mirroring stdout/stderr into a
project-local log session under .logs/local/.

Environment:
  LOCAL_LOG_BASE              Override log base directory (default: .logs/local)
  LOCAL_LOG_MAX_BYTES         Per log chunk max bytes (default: 1048576)
  LOCAL_LOG_MAX_CHUNKS        Per service chunks to keep in a session (default: 16)
  LOCAL_LOG_FLUSH_LINES       Flush file output every N lines (default: 50)
  LOCAL_LOG_CONSOLE           Mirror logs to console as well as file (default: true)
  LOCAL_LOG_KEEP_SESSIONS     Number of session directories to keep (default: 3)
  LOCAL_LOG_SESSION           Force a session name for multiple commands
  LOCAL_LOG_SESSION_TTL_SECONDS
                              Reuse a freshly-created session for parallel tasks
                              (default: 120)
  LOCAL_LOG_LOCK_STALE_SECONDS
                              Recover a stale session lock after N seconds
                              (default: 30)
USAGE
}

if [[ $# -lt 3 || "${2:-}" != "--" ]]; then
  usage
  exit 2
fi

service_name=$1
shift 2

if [[ $service_name =~ [^A-Za-z0-9_.-] ]]; then
  echo "ERROR: invalid service name: $service_name" >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd -- "$script_dir/../.." && pwd)
log_base=${LOCAL_LOG_BASE:-"$project_root/.logs/local"}
max_bytes=${LOCAL_LOG_MAX_BYTES:-1048576}
max_chunks=${LOCAL_LOG_MAX_CHUNKS:-16}
flush_lines=${LOCAL_LOG_FLUSH_LINES:-50}
console_enabled=${LOCAL_LOG_CONSOLE:-true}
keep_sessions=${LOCAL_LOG_KEEP_SESSIONS:-3}
session_ttl=${LOCAL_LOG_SESSION_TTL_SECONDS:-120}
lock_stale_seconds=${LOCAL_LOG_LOCK_STALE_SECONDS:-30}
current_file="$log_base/.current-session"
lock_dir="$log_base/.lock"
session_name_re='^[A-Za-z0-9_.-]+$'

if ! [[ $max_bytes =~ ^[0-9]+$ ]] || (( max_bytes < 1024 )); then
  echo "ERROR: LOCAL_LOG_MAX_BYTES must be an integer >= 1024" >&2
  exit 2
fi
if ! [[ $max_chunks =~ ^[0-9]+$ ]] || (( max_chunks < 1 )); then
  echo "ERROR: LOCAL_LOG_MAX_CHUNKS must be an integer >= 1" >&2
  exit 2
fi
if ! [[ $flush_lines =~ ^[0-9]+$ ]] || (( flush_lines < 1 )); then
  echo "ERROR: LOCAL_LOG_FLUSH_LINES must be an integer >= 1" >&2
  exit 2
fi
if ! [[ $keep_sessions =~ ^[0-9]+$ ]] || (( keep_sessions < 1 )); then
  echo "ERROR: LOCAL_LOG_KEEP_SESSIONS must be an integer >= 1" >&2
  exit 2
fi
if ! [[ $session_ttl =~ ^[0-9]+$ ]]; then
  echo "ERROR: LOCAL_LOG_SESSION_TTL_SECONDS must be an integer" >&2
  exit 2
fi
if ! [[ $lock_stale_seconds =~ ^[0-9]+$ ]] || (( lock_stale_seconds < 1 )); then
  echo "ERROR: LOCAL_LOG_LOCK_STALE_SECONDS must be an integer >= 1" >&2
  exit 2
fi

mkdir -p "$log_base"

acquire_lock() {
  while ! mkdir "$lock_dir" 2>/dev/null; do
    if [[ -d $lock_dir ]]; then
      local now lock_mtime
      now=$(date -u +%s)
      lock_mtime=$(stat -c %Y "$lock_dir" 2>/dev/null || echo "$now")
      if [[ $lock_mtime =~ ^[0-9]+$ ]] && (( now - lock_mtime > lock_stale_seconds )); then
        rm -rf -- "$lock_dir"
        continue
      fi
    fi
    sleep 0.05
  done
}

release_lock() {
  rmdir "$lock_dir" 2>/dev/null || true
}

cleanup_old_sessions() {
  find "$log_base" -mindepth 1 -maxdepth 1 -type d ! -name '.lock' -printf '%T@ %p\n' \
    | sort -rn \
    | awk -v keep="$keep_sessions" 'NR > keep {print substr($0, index($0, $2))}' \
    | while IFS= read -r old_session; do
        [[ -n $old_session ]] || continue
        if session_has_live_process "$old_session"; then
          continue
        fi
        rm -rf -- "$old_session"
      done
}

session_has_live_process() {
  local session_dir=$1
  local marker pid
  for marker in "$session_dir"/.active.*; do
    [[ -e $marker ]] || continue
    pid=""
    read -r pid < "$marker" || true
    if [[ $pid =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    rm -f -- "$marker"
  done
  return 1
}

select_session() {
  local now created existing
  now=$(date -u +%s)

  if [[ -n "${LOCAL_LOG_SESSION:-}" ]]; then
    if ! [[ $LOCAL_LOG_SESSION =~ $session_name_re ]]; then
      echo "ERROR: LOCAL_LOG_SESSION contains unsafe characters" >&2
      exit 2
    fi
    session=$LOCAL_LOG_SESSION
    mkdir -p "$log_base/$session"
    printf '%s %s\n' "$session" "$now" > "$current_file"
    ln -sfn "$session" "$log_base/latest"
    return
  fi

  if [[ -f $current_file ]]; then
    read -r existing created < "$current_file" || true
    if [[ -n ${existing:-} && -n ${created:-} && $existing =~ $session_name_re ]]; then
      if [[ -d "$log_base/$existing" && $created =~ ^[0-9]+$ ]] && (( now - created <= session_ttl )); then
        session=$existing
        ln -sfn "$session" "$log_base/latest"
        return
      fi
    fi
  fi

  session=$(date -u +%Y%m%dT%H%M%SZ)-$$
  mkdir -p "$log_base/$session"
  printf '%s %s\n' "$session" "$now" > "$current_file"
  ln -sfn "$session" "$log_base/latest"
}

acquire_lock
trap release_lock EXIT
select_session
session_dir="$log_base/$session"
mkdir -p "$session_dir"
active_marker="$session_dir/.active.$service_name.$$"
printf '%s\n' "$$" > "$active_marker"
cleanup_old_sessions
release_lock
trap - EXIT

log_file="$session_dir/$service_name.log"

find "$session_dir" -maxdepth 1 -type f -name "$service_name.log.*" -printf '%f\n' \
  | while IFS= read -r chunk_file; do
      suffix=${chunk_file#"$service_name.log."}
      if [[ $suffix =~ ^[0-9]+$ ]] && (( suffix >= max_chunks )); then
        rm -f -- "$session_dir/$chunk_file"
      fi
    done

start_chunk=0
current_log_file=$log_file
while [[ -e $current_log_file ]] && (( $(wc -c < "$current_log_file") >= max_bytes )); do
  if (( start_chunk + 1 >= max_chunks )); then
    start_chunk=0
    current_log_file=$log_file
    : > "$current_log_file"
    break
  fi
  start_chunk=$((start_chunk + 1))
  current_log_file="$log_file.$start_chunk"
done
initial_bytes=0
if [[ -e $current_log_file ]]; then
  initial_bytes=$(wc -c < "$current_log_file")
fi

run_pipe_dir=$(mktemp -d "$log_base/.run-with-log.$service_name.XXXXXX")
run_pipe="$run_pipe_dir/output.fifo"
mkfifo "$run_pipe"
exec {run_pipe_guard_fd}<>"$run_pipe"
cmd_pid=""
log_pid=""
used_setsid=false

cleanup_pipe() {
  rm -rf -- "$run_pipe_dir"
}

cleanup_active_marker() {
  rm -f -- "${active_marker:-}"
}

terminate_wrapped_command() {
  local signal=${1:-TERM}
  trap - INT TERM HUP
  if [[ -n "${cmd_pid:-}" ]] && kill -0 "$cmd_pid" 2>/dev/null; then
    if [[ "$used_setsid" == true ]]; then
      kill -"$signal" -- "-$cmd_pid" 2>/dev/null || true
    else
      kill -"$signal" -- "$cmd_pid" 2>/dev/null || true
    fi
    for _attempt in 1 2 3 4 5; do
      kill -0 "$cmd_pid" 2>/dev/null || break
      sleep 0.2
    done
    if kill -0 "$cmd_pid" 2>/dev/null; then
      if [[ "$used_setsid" == true ]]; then
        kill -KILL -- "-$cmd_pid" 2>/dev/null || true
      else
        kill -KILL -- "$cmd_pid" 2>/dev/null || true
      fi
    fi
  fi
}

handle_signal() {
  local signal=$1
  terminate_wrapped_command "$signal"
  wait "${cmd_pid:-}" 2>/dev/null || true
  wait "${log_pid:-}" 2>/dev/null || true
  cleanup_pipe
  cleanup_active_marker
  case "$signal" in
    INT) exit 130 ;;
    HUP) exit 129 ;;
    *) exit 143 ;;
  esac
}

trap cleanup_pipe EXIT
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM
trap 'handle_signal HUP' HUP

LC_ALL=C awk \
  -v base="$log_file" \
  -v max="$max_bytes" \
  -v max_chunks="$max_chunks" \
  -v flush_lines="$flush_lines" \
  -v console_enabled="$console_enabled" \
  -v start_chunk="$start_chunk" \
  -v initial_bytes="$initial_bytes" '
  BEGIN {
    chunk = start_chunk + 0
    file = chunk == 0 ? base : base "." chunk
    bytes = initial_bytes + 0
    dirty_lines = 0
    total_lines = 0
  }
  function rotate() {
    close(file)
    chunk += 1
    if (chunk >= max_chunks) {
      chunk = 0
    }
    file = base "." chunk
    if (chunk == 0) {
      file = base
    }
    printf "" > file
    close(file)
    bytes = 0
    dirty_lines = 0
  }
  function write_chunk(text, part, room) {
    while (length(text) > 0) {
      if (bytes >= max) {
        rotate()
      }
      room = max - bytes
      part = substr(text, 1, room)
      printf "%s", part >> file
      bytes += length(part)
      text = substr(text, length(part) + 1)
      if (bytes >= max && length(text) > 0) {
        rotate()
      }
    }
    total_lines += 1
    dirty_lines += 1
    if (total_lines <= 5 || dirty_lines >= flush_lines) {
      fflush(file)
      dirty_lines = 0
    }
  }
  {
    if (console_enabled != "false" && console_enabled != "0" && console_enabled != "no") {
      print
    }
    line = $0 "\n"
    write_chunk(line)
  }
  END {
    fflush(file)
  }
' < "$run_pipe" {run_pipe_guard_fd}>&- &
log_pid=$!

set +e
if command -v setsid >/dev/null 2>&1; then
  {
    printf "==> local log session: %s\n" "$session"
    printf "==> local log file: %s\n" "$current_log_file"
    printf "==> local log max bytes: %s\n" "$max_bytes"
    printf "==> local log max chunks: %s\n" "$max_chunks"
    printf "==> command:"
    printf " %q" "$@"
    printf "\n"
    exec setsid "$@"
  } > "$run_pipe" 2>&1 {run_pipe_guard_fd}>&- &
  used_setsid=true
else
  {
    printf "==> local log session: %s\n" "$session"
    printf "==> local log file: %s\n" "$current_log_file"
    printf "==> local log max bytes: %s\n" "$max_bytes"
    printf "==> local log max chunks: %s\n" "$max_chunks"
    printf "==> command:"
    printf " %q" "$@"
    printf "\n"
    exec "$@"
  } > "$run_pipe" 2>&1 {run_pipe_guard_fd}>&- &
fi
cmd_pid=$!
exec {run_pipe_guard_fd}>&-

wait "$cmd_pid"
exit_code=$?
wait "$log_pid" 2>/dev/null || true
set -e

trap - INT TERM HUP EXIT
cleanup_pipe
cleanup_active_marker

exit "$exit_code"
