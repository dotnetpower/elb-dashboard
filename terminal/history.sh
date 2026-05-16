#!/bin/bash
# Per-shell audit history for the browser terminal sidecar.
#
# Every interactive bash session inside the `terminal` sidecar writes its
# command history to:
#   $HOME/.elb-history/commands.<PID>.log   (one file per shell, bash
#                                            native HISTFILE format with
#                                            "#<unix_ts>" timestamp lines)
#   $HOME/.elb-history/sessions.log         (one tab-separated line per
#                                            shell start / shell exit)
#
# In production the operator's $HOME is mounted from the `terminal-home`
# Azure Files share, so the logs persist across Container App revisions
# and replicas. In local compose the home is container-local, which is
# fine for development — restarting the sidecar drops the logs.
#
# Notes:
#   * Each shell session gets its own file (HISTFILE includes $$) so
#     concurrent operators do not interleave commands.
#   * `PROMPT_COMMAND="history -a"` flushes every command to disk
#     immediately, so a session crash / WebSocket drop never loses the
#     trail of what was just typed.
#   * `HISTCONTROL` / `HISTIGNORE` are deliberately unset — audit must
#     capture EVERY command including duplicates, leading-space tricks,
#     and `cd` / `ls`.

[[ $- == *i* ]] || return 0

ELB_HISTORY_DIR="${ELB_HISTORY_DIR:-$HOME/.elb-history}"
if ! mkdir -p "$ELB_HISTORY_DIR" 2>/dev/null; then
  return 0
fi
chmod 0700 "$ELB_HISTORY_DIR" 2>/dev/null || true

export HISTFILE="$ELB_HISTORY_DIR/commands.$$.log"
export HISTSIZE=10000
export HISTFILESIZE=100000
export HISTTIMEFORMAT='%FT%T%z  '
unset HISTCONTROL HISTIGNORE
shopt -s histappend cmdhist 2>/dev/null || true

case ";${PROMPT_COMMAND:-};" in
  *';history -a;'*|*'; history -a;'*) ;;
  *) PROMPT_COMMAND="history -a${PROMPT_COMMAND:+; $PROMPT_COMMAND}" ;;
esac

if [[ -z "${ELB_HISTORY_SESSION_LOGGED:-}" ]]; then
  export ELB_HISTORY_SESSION_LOGGED=1

  # Capture tty separately: when the shell has no controlling terminal
  # (e.g. the test harness runs us with stdin=/dev/null) `tty` prints
  # "not a tty" to STDOUT before exiting non-zero, which would slip a
  # newline into the audit line and split it in two. Discard stdout on
  # failure and substitute a literal placeholder.
  ELB_HISTORY_TTY="$(tty 2>/dev/null)" || ELB_HISTORY_TTY="unknown"
  ELB_HISTORY_DATE="$(date -Iseconds 2>/dev/null)" || ELB_HISTORY_DATE="$(date)"

  # Force restrictive perms on sessions.log even when the inherited
  # umask would normally create it 0644. The Azure Files share mounted
  # at $HOME may be visible to other tools/sidecars, so we make the
  # audit ledger owner-only just like commands.<PID>.log (which bash
  # already creates 0600 via its HISTFILE writer).
  (
    umask 0177
    {
      printf '%s\tpid=%s\ttty=%s\tuser=%s\tstart\n' \
        "$ELB_HISTORY_DATE" "$$" "$ELB_HISTORY_TTY" "${USER:-unknown}"
    } >> "$ELB_HISTORY_DIR/sessions.log" 2>/dev/null
  )
  chmod 0600 "$ELB_HISTORY_DIR/sessions.log" 2>/dev/null || true

  # shellcheck disable=SC2064  # $$ must be expanded NOW so the trap
  # records the right PID even after subshells set their own.
  trap "( umask 0177; printf '%s\tpid=%s\tend\n' \"\$(date -Iseconds 2>/dev/null || date)\" $$ >> '$ELB_HISTORY_DIR/sessions.log' 2>/dev/null )" EXIT
fi
