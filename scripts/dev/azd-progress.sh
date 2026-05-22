#!/usr/bin/env bash
# Print consistent azd up progress markers for hooks and wrapper scripts.

set -euo pipefail

cmd="${1:-plan}"
shift || true
style="${ELB_DEPLOY_STYLE:-pretty}"
use_color=false

if [[ -t 1 && -z "${NO_COLOR:-}" && "${CI:-}" != "true" && "$style" != "plain" ]]; then
  use_color=true
fi

timestamp() {
  date -u +%H:%M:%S
}

paint() {
  local code="$1"
  shift
  if $use_color; then
    printf '\033[%sm%s\033[0m' "$code" "$*"
  else
    printf '%s' "$*"
  fi
}

bold() { paint '1' "$*"; }
dim() { paint '2' "$*"; }
cyan() { paint '36' "$*"; }
green() { paint '32' "$*"; }
yellow() { paint '33' "$*"; }
red() { paint '31' "$*"; }

rule() {
  dim '------------------------------------------------------------'
  printf '\n'
}

print_row() {
  local number="$1"
  local title="$2"
  local detail="$3"
  printf '  %s  %-24s %s\n' "$(cyan "$number/8")" "$title" "$(dim "$detail")"
}

case "$cmd" in
  plan)
    printf '\n'
    rule
    bold 'elb-dashboard deploy'
    printf '\n'
    dim 'azd up progress map'
    printf '\n'
    rule
    print_row 0 'Local bootstrap' 'login, azd env, env values'
    print_row 1 'Provider registration' 'required Azure providers'
    print_row 2 'Resource group choice' 'reuse, delete, or choose numbered RG'
    print_row 3 'Bicep provision' 'RG, VNet, identity, ACR, Storage, Key Vault, Container Apps Environment'
    print_row 4 'App registration' 'create/reuse SPA/API App Registration'
    print_row 5 'Resource validation' 'Storage HNS and workspace tags'
    print_row 6 'Image builds' 'api, frontend, terminal via az acr build'
    print_row 7 'Sidecar swap' 'replace bootstrap app with six-sidecar layout'
    print_row 8 'Health check' 'wait for /api/health and print URL'
    rule
    printf '\n'
    ;;
  step)
    number="${1:?step number required}"
    title="${2:?step title required}"
    detail="${3:-}"
    printf '[%s] %s %s  %-24s' "$(timestamp)" "$(cyan '>')" "$(bold "$number/8")" "$title"
    if [[ -n "$detail" ]]; then
      printf ' %s' "$detail"
    fi
    printf '\n'
    ;;
  done)
    number="${1:?step number required}"
    title="${2:?step title required}"
    printf '[%s] %s %s  %-24s %s\n' "$(timestamp)" "$(green '+')" "$(bold "$number/8")" "$title" "$(green 'done')"
    ;;
  note)
    message="${1:?message required}"
    printf '[%s] %s %s\n' "$(timestamp)" "$(yellow '-')" "$(dim "$message")"
    ;;
  *)
    printf '%s unknown progress command: %s\n' "$(red 'ERROR:')" "$cmd" >&2
    exit 2
    ;;
esac
