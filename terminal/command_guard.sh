#!/bin/bash
# Guard interactive terminal sessions from common destructive commands.

__elb_terminal_guard_reason() {
  local command_text="$1"
  command_text="${command_text//$'\n'/ }"
  command_text="${command_text//$'\t'/ }"

  case "$command_text" in
    __elb_terminal_guard*|__elb_terminal_command_allowed*) return 1 ;;
    "") return 1 ;;
  esac

  case "$command_text" in
    "trap - DEBUG"*|"trap -- - DEBUG"*|"trap '' DEBUG"*|"trap \"\" DEBUG"*)
      printf '%s' "disabling the terminal safety guard"
      return 0
      ;;
    "shopt -u extdebug"*|"set +o functrace"*|"set +T"*|"unset -f __elb_terminal_guard"*)
      printf '%s' "disabling the terminal safety guard"
      return 0
      ;;
    "export ELB_TERMINAL_GUARD=0"*|"ELB_TERMINAL_GUARD=0 "*)
      printf '%s' "disabling the terminal safety guard"
      return 0
      ;;
  esac

  # Detect any sudo invocation: bare `sudo`, alias-bypassing `\sudo`, or
  # builtin-bypassing `command sudo`. The OS-level defense lives in
  # /etc/sudoers.d/azureuser-apt (NOPASSWD only for apt-get/apt
  # update|install) — this guard just gives an immediate, clear error
  # message instead of waiting for sudo to fail. Note: `[\\]?` is used
  # instead of `(\\)?` because the latter would be re-quoted by bash
  # before reaching the ERE engine and lose the backslash.
  if [[ "$command_text" =~ (^|[^a-zA-Z0-9_./-])[\\]?sudo([[:space:]]|$) ]] \
    || [[ "$command_text" =~ (^|[[:space:];|&])command[[:space:]]+sudo([[:space:]]|$) ]]; then
    # Allow `sudo apt update` / `sudo apt install ...` (and the `apt-get`
    # aliases) when they are the LEADING command. The matching sudoers
    # drop-in (terminal/Dockerfile -> /etc/sudoers.d/azureuser-apt) only
    # NOPASSWD-permits these subcommands, so any other sudo invocation
    # would prompt for a password the operator does not have anyway —
    # this guard rule just gives a clearer error message and keeps
    # destructive subcommands (remove/purge/dist-upgrade/dpkg) blocked
    # even if someone later relaxes the sudoers file.
    if [[ "$command_text" =~ ^[[:space:]]*[\\]?sudo[[:space:]]+(apt|apt-get)[[:space:]]+(update|install)([[:space:]]|$) ]]; then
      :  # leading `sudo apt[-get] (update|install) ...` — allow, fall through
    else
      printf '%s' "sudo is restricted to 'apt update' and 'apt install' in the browser terminal"
      return 0
    fi
  fi

  if [[ "$command_text" =~ (^|[[:space:]])(/usr/bin/|/bin/)?(shutdown|reboot|poweroff|halt)([[:space:]]|$) ]]; then
    printf '%s' "host shutdown commands are blocked"
    return 0
  fi

  if [[ "$command_text" =~ (^|[[:space:]])(/usr/sbin/|/sbin/|/usr/bin/|/bin/)?(mkfs|fdisk|parted|wipefs)([.[:alnum:]_-]*)([[:space:]]|$) ]]; then
    printf '%s' "disk formatting and partitioning commands are blocked"
    return 0
  fi

  if [[ "$command_text" =~ (^|[[:space:]])(/usr/bin/|/bin/)?dd([[:space:]]|$) ]] && [[ "$command_text" =~ [[:space:]]of=/dev/ ]]; then
    printf '%s' "raw writes to block devices are blocked"
    return 0
  fi

  if [[ "$command_text" =~ (^|[[:space:]])(/usr/bin/|/bin/)?(bash|sh|dash)([[:space:]]+-(c|s)|[[:space:]]*$) ]]; then
    printf '%s' "piped or inline shell execution is blocked"
    return 0
  fi

  if [[ "$command_text" =~ (^|[[:space:];|&])(/usr/bin/|/bin/)?rm([[:space:]]|$) ]] \
    && [[ "$command_text" =~ (^|[[:space:]])-[^[:space:]]*[rR] ]] \
    && [[ "$command_text" =~ (^|[[:space:]])(/|/\*|/home|/home/azureuser|/opt|/usr|/etc|/var|/dev|/proc|/sys|~|\$HOME|\"\$HOME\"|'\$HOME')([[:space:]]|$) ]]; then
    printf '%s' "recursive deletion of protected paths is blocked"
    return 0
  fi

  if [[ "$command_text" =~ (^|[[:space:];|&])(/usr/bin/|/bin/)?rm([[:space:]]|$) ]] \
    && [[ "$command_text" =~ (^|[[:space:]])-[^[:space:]]*[rR] ]] \
    && [[ "$command_text" =~ (^|[[:space:]])(\.|\.\/|\*|\.\*|--[[:space:]]+\.|--[[:space:]]+\.\/|--[[:space:]]+\*|--[[:space:]]+\.\*)([[:space:]]|$) ]]; then
    printf '%s' "recursive deletion of the current workspace is blocked"
    return 0
  fi

  if [[ "$command_text" =~ (^|[[:space:]])(/usr/bin/|/bin/)?find[[:space:]]+/[^[:space:]]*.*[[:space:]]-delete([[:space:]]|$) ]]; then
    printf '%s' "bulk deletion from root paths is blocked"
    return 0
  fi

  if [[ "$command_text" =~ (^|[[:space:]])az[[:space:]].*[[:space:]]delete([[:space:]]|$) ]]; then
    printf '%s' "Azure delete operations must be run through the dashboard workflow"
    return 0
  fi

  # elastic-blast / elb `delete` tears down the AKS cluster and all results
  # for the run — the same destructive-infra class as `az group delete`.
  # Only the `delete` subcommand is gated; submit/status/run stay allowed.
  # The option-chain `([[:space:]][^[:space:]]+)*` lets global flags precede
  # the subcommand (e.g. `elastic-blast --loglevel DEBUG delete`) while the
  # explicit `[[:space:]]delete` word boundary avoids matching a "delete"
  # substring inside a path/URL argument of submit.
  if [[ "$command_text" =~ (^|[[:space:];|&])(elastic-blast|elb)([[:space:]][^[:space:]]+)*[[:space:]]delete([[:space:]]|$) ]]; then
    printf '%s' "elastic-blast delete tears down the cluster and all results; run it from the dashboard BLAST workflow"
    return 0
  fi

  if [[ "$command_text" =~ (^|[[:space:]])kubectl[[:space:]].*delete[[:space:]].*(namespace|ns|node|clusterrole|clusterrolebinding|crd|customresourcedefinition|pv|storageclass)([[:space:]]|$) ]]; then
    printf '%s' "cluster-level kubectl delete operations are blocked"
    return 0
  fi

  if [[ "$command_text" =~ (^|[[:space:]])kubectl[[:space:]].*delete[[:space:]].*(--all|all)([[:space:]]|$) ]]; then
    printf '%s' "bulk kubectl delete operations are blocked"
    return 0
  fi

  return 1
}

__elb_terminal_guard() {
  [[ "${__ELB_TERMINAL_GUARD_INTERNAL:-0}" == "1" ]] && return 0

  local reason
  local rc
  __ELB_TERMINAL_GUARD_INTERNAL=1
  reason="$(__elb_terminal_guard_reason "$BASH_COMMAND")"
  rc=$?
  __ELB_TERMINAL_GUARD_INTERNAL=0
  [[ "$rc" -ne 0 ]] && return 0
  printf '\nELB terminal guard blocked: %s\n' "$reason" >&2
  printf 'Use the dashboard workflow for destructive infrastructure changes.\n\n' >&2
  return 1
}

__elb_terminal_command_allowed() {
  local command_text="$1"
  local rc
  __ELB_TERMINAL_GUARD_INTERNAL=1
  __elb_terminal_guard_reason "$command_text" >/dev/null
  rc=$?
  __ELB_TERMINAL_GUARD_INTERNAL=0
  [[ "$rc" -ne 0 ]]
}

if [[ "${ELB_TERMINAL_GUARD:-1}" != "0" ]]; then
  if [[ $- == *i* || "${ELB_TERMINAL_GUARD_TEST:-0}" == "1" ]]; then
    shopt -s extdebug
    trap __elb_terminal_guard DEBUG
  fi
fi