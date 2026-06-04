#!/bin/bash
# Render the interactive browser-terminal login banner.

set -uo pipefail

RESET=$'\033[0m'
BOLD=$'\033[1m'
ITALIC=$'\033[3m'
DIM=$'\033[2m'
HIDE_CURSOR=$'\033[?25l'
SHOW_CURSOR=$'\033[?25h'
CYAN=$'\033[38;5;51m'
BLUE=$'\033[38;5;39m'
INDIGO=$'\033[38;5;63m'
MAGENTA=$'\033[38;5;201m'
AQUA_TRUE=$'\033[38;2;76;222;255m'
BLUE_TRUE=$'\033[38;2;122;167;255m'
VIOLET_TRUE=$'\033[38;2;168;120;255m'
PINK_TRUE=$'\033[38;2;255;92;205m'
MUTED=$'\033[38;5;244m'
GREEN=$'\033[38;5;120m'

supports_colour() {
  [[ -z "${NO_COLOR:-}" ]] || return 1
  [[ "${TERM:-}" != "dumb" ]] || return 1
  [[ -t 1 || "${ELB_TERMINAL_BANNER_FORCE_COLOR:-0}" == "1" ]]
}

render_title_block() {
  # All rows align the right-side text at column 16 (three cells past the
  # underscore tail) so nothing overlaps the "▄▄▄▄▄" baseline. "██" steps
  # inward then outward two cells per row to draw a wider chevron. The
  # right-side text follows a strict JetBrains-CLI style: labels are BOLD
  # WHITE, only the wordmark and the status accents ("protected shell"
  # green, trace start/end) carry colour.
  printf '  %s██%s           %s%sElasticBlast%s %s%sCLI%s  %s%sv0.1%s\n' \
    "$AQUA_TRUE" "$RESET" \
    "$BOLD$ITALIC$AQUA_TRUE" "$BOLD$ITALIC$VIOLET_TRUE" "$RESET" \
    "$BOLD$ITALIC$PINK_TRUE" "$BOLD$ITALIC$PINK_TRUE" "$RESET" \
    "$DIM" "$MUTED" "$RESET"
  printf '    %s██%s         %sSigned in with Azure%s  %s/session%s\n' \
    "$BLUE_TRUE" "$RESET" "$BOLD" "$RESET" "$MUTED" "$RESET"
  printf '      %s██%s       %sGuard:%s %sprotected shell%s  %s/help%s\n' \
    "$VIOLET_TRUE" "$RESET" "$BOLD" "$RESET" "$GREEN" "$RESET" "$MUTED" "$RESET"
  printf '    %s██%s         %sTrace:%s %sbrowser%s %s>>>%s api %s>>>%s ttyd %s>>>%s %sshell%s\n' \
    "$VIOLET_TRUE" "$RESET" "$BOLD" "$RESET" \
    "$CYAN" "$RESET" "$MUTED" "$RESET" "$MUTED" "$RESET" \
    "$MUTED" "$RESET" "$MAGENTA" "$RESET"
  printf '  %s██%s     %s▄▄▄▄▄%s\n' \
    "$PINK_TRUE" "$RESET" "$PINK_TRUE" "$RESET"
  printf '\n'
  printf '  %sAudit:%s every command in this session is recorded to %s~/.elb-history/commands.<pid>.log%s\n' \
    "$BOLD" "$RESET" "$MUTED" "$RESET"
  printf '         %s(by using this terminal you consent to this logging)%s\n' \
    "$DIM$MUTED" "$RESET"
}

render_onboarding() {
  # First-run guidance + the load-bearing warning that $HOME does not persist
  # across revision restarts. Shown once per interactive session (profile.sh
  # guards on ELB_MOTD_SHOWN).
  printf '\n'
  printf '  %sGet started%s\n' "$BOLD" "$RESET"
  printf '    %s1.%s %saz login --use-device-code%s   %s# attribute cloud actions to you%s\n' \
    "$BOLD" "$RESET" "$CYAN" "$RESET" "$DIM$MUTED" "$RESET"
  printf '    %s2.%s %selb-cfg --program blastn -o ~/elastic-blast.ini%s   %s# scaffold a config%s\n' \
    "$BOLD" "$RESET" "$CYAN" "$RESET" "$DIM$MUTED" "$RESET"
  printf '    %s3.%s %selb-cfg --check ~/elastic-blast.ini%s   %s# validate before submit%s\n' \
    "$BOLD" "$RESET" "$CYAN" "$RESET" "$DIM$MUTED" "$RESET"
  printf '       %sExamples and a template are in %s~/examples/%s%s.\n' \
    "$DIM$MUTED" "$RESET$MUTED" "$DIM$MUTED" "$RESET"
  printf '\n'
  printf '  %s⚠ Ephemeral home:%s %s$HOME is wiped on every revision restart.%s\n' \
    "$BOLD$PINK_TRUE" "$RESET" "$BOLD" "$RESET"
  printf '         %sStage inputs/outputs to Storage with %sazcopy%s%s; do not keep the only copy here.%s\n' \
    "$MUTED" "$CYAN" "$RESET$MUTED" "$MUTED" "$RESET"
}

render_compact_banner() {
  render_title_block
  render_onboarding
}

render_plain() {
  cat "${ELB_TERMINAL_MOTD_PATH:-/etc/motd}" 2>/dev/null || true
}

render_colour() {
  local animate="${ELB_TERMINAL_BANNER:-animated}"
  if [[ "$animate" == "animated" && "${ELB_TERMINAL_BANNER_FORCE_COLOR:-0}" != "1" ]]; then
    printf '%s' "$HIDE_CURSOR"
    for prompt in '▛' '▛▀▀▙' '▛▀▀▙ ╱ ElasticBlast CLI'; do
      printf '  %s%s%s %sopening ElasticBLAST CLI%s\n' "$BOLD$AQUA_TRUE" "$prompt" "$RESET" "$DIM$BLUE_TRUE" "$RESET"
      sleep 0.035
      printf '\033[1A\033[J'
    done
    printf '%s' "$SHOW_CURSOR"
  fi

  render_compact_banner
}

main() {
  if supports_colour; then
    render_colour
  else
    render_plain
  fi
}

main "$@"