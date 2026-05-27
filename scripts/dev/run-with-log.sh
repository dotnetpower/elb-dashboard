#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: scripts/dev/run-with-log.sh <service-name> -- <command> [args...]

Runs a local development command while mirroring stdout/stderr into a single
project-local log file:

  .logs/local/latest/<service-name>.log

Logs are appended across runs and rotated as a bounded ring per service so the
directory stays small and discoverable. There is exactly one log location —
no session folders, no symlinks, no per-invocation subdirectories.

Environment:
  LOCAL_LOG_BASE          Override log base directory (default: .logs/local)
  LOCAL_LOG_MAX_BYTES     Per log chunk max bytes (default: 1048576 = 1 MiB)
  LOCAL_LOG_MAX_CHUNKS    Chunks kept per service in the ring (default: 5)
  LOCAL_LOG_FLUSH_LINES   Flush file output every N lines (default: 50)
  LOCAL_LOG_CONSOLE       Mirror to console too (default: true)
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
max_chunks=${LOCAL_LOG_MAX_CHUNKS:-5}
flush_lines=${LOCAL_LOG_FLUSH_LINES:-50}
console_enabled=${LOCAL_LOG_CONSOLE:-true}

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

# Single fixed log directory. If `latest` is still a leftover symlink from the
# old session-folder layout, replace it with a real directory so we never
# accidentally tail across sessions.
mkdir -p "$log_base"
if [[ -L "$log_base/latest" ]]; then
  rm -f "$log_base/latest"
fi
mkdir -p "$log_base/latest"
log_dir="$log_base/latest"
log_file="$log_dir/$service_name.log"

# Drop any chunk files that exceed the current ring size (e.g. user lowered
# LOCAL_LOG_MAX_CHUNKS, or the legacy run-with-log.sh wrote .log.6+).
find "$log_dir" -maxdepth 1 -type f -name "$service_name.log.*" -printf '%f\n' \
  | while IFS= read -r chunk_file; do
      suffix=${chunk_file#"$service_name.log."}
      if [[ $suffix =~ ^[0-9]+$ ]] && (( suffix >= max_chunks )); then
        rm -f -- "$log_dir/$chunk_file"
      fi
    done

# Pick up the rotation cursor where we left off: find the highest existing
# chunk index (0 == base file) and seed `start_chunk` so a long debugging
# session resumes appending into the most recent chunk instead of clobbering
# `<service>.log` on every start.
start_chunk=0
current_log_file=$log_file
for ((chunk_idx = max_chunks - 1; chunk_idx >= 0; chunk_idx--)); do
  if (( chunk_idx == 0 )); then
    candidate=$log_file
  else
    candidate="$log_file.$chunk_idx"
  fi
  if [[ -e $candidate ]]; then
    start_chunk=$chunk_idx
    current_log_file=$candidate
    break
  fi
done

# If the resume chunk is already full, advance the cursor so the new run does
# not push the partial line over the size cap on its very first write.
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

terminate_wrapped_command() {
  local signal=${1:-TERM}
  trap - INT TERM HUP
  if [[ -n "${cmd_pid:-}" ]] && kill -0 "$cmd_pid" 2>/dev/null; then
    kill -"$signal" -- "$cmd_pid" 2>/dev/null || true
    for _attempt in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$cmd_pid" 2>/dev/null || break
      sleep 0.5
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
    printf "==> local log file: %s\n" "$current_log_file"
    printf "==> rotation: %s bytes x %s chunks\n" "$max_bytes" "$max_chunks"
    printf "==> command:"
    printf " %q" "$@"
    printf "\n"
    exec setsid "$@"
  } > "$run_pipe" 2>&1 {run_pipe_guard_fd}>&- &
  used_setsid=true
else
  {
    printf "==> local log file: %s\n" "$current_log_file"
    printf "==> rotation: %s bytes x %s chunks\n" "$max_bytes" "$max_chunks"
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

exit "$exit_code"
