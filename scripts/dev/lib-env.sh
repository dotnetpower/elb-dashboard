#!/usr/bin/env bash
# Shared environment-loading helpers for the dev/deploy bash scripts.
#
# Responsibility: provide ONE correct implementation of the ".env / azd env
# get-values -> process environment" import used by quick-deploy.sh,
# cli-upgrade.sh, and setup-gha-oidc.sh. Centralising it prevents the guard
# from drifting back to the buggy `${!key:-}` form (see below).
#
# Edit boundaries: pure functions over the process environment + files. No
# Azure calls beyond `azd env get-values`. No side effects on sourcing other
# than defining functions and a re-source guard.
#
# Key entry points:
#   strip_quotes <value>                       -> echoes value with one layer of surrounding "" removed
#   load_simple_env_file <file> [SKIP_KEY...]  -> export KEY=VALUE lines that are currently UNSET
#   load_azd_env                               -> export `azd env get-values` keys that are currently UNSET
#                                                 (falls back to the env's .env file when the CLI yields nothing)
#
# Risky contracts:
#   * The "currently unset" test MUST use `${!key+x}` (set-vs-unset), NOT
#     `${!key:-}` (empty-OR-unset). A caller that deliberately exports an
#     empty string (e.g. `VITE_API_BASE_URL=""` or `VITE_AUTH_DEV_BYPASS=`
#     before a cloud frontend deploy) relies on that empty value being
#     PRESERVED, not silently overwritten by a value in web/.env.local.
#     Regressing to `${!key:-}` re-introduces the 2026-05-21 / 2026-05-25
#     frontend env-leak incidents (dev auth bypass / localhost API URL baked
#     into a cloud SPA). See
#     docs/features_change/2026-05/2026-05-25-frontend-env-leak-hardening.md.
#   * SKIP keys are never imported from the file even if unset — used to keep
#     web/.env.local local-dev toggles out of cloud deploys.
#   * load_azd_env MUST stay resilient to a slow/absent/prompting `azd` CLI:
#     when `azd env get-values` yields no usable KEY=VALUE data (azd missing,
#     not logged in, killed by the timeout, or blocked on the "Select an
#     environment" prompt — all observed in practice) it falls back to reading
#     `.azure/<env>/.env` directly, so a per-deployment control-plane pin
#     stored only in azd env (e.g. SERVICEBUS_ENABLED=true) still reaches the
#     deploy. The "usable" test checks for an actual assignment line, not just
#     non-empty output, because a killed prompt leaves prompt text on stdout.
#     Removing that fallback re-opens the redeploy-resets-toggle bug class
#     (see docs/features_change/2026-06/ for the Service Bus case).
#
# Validation: `bash -n scripts/dev/lib-env.sh`; the consuming scripts run it
# on every deploy. A regression test lives in
# scripts/dev/tests/test_lib_env.sh.

# Re-source guard: defining the functions twice is harmless, but the guard
# keeps `source` cheap when several scripts pull this in transitively.
if [[ -n "${_ELB_LIB_ENV_SOURCED:-}" ]]; then
  return 0 2>/dev/null || true
fi
_ELB_LIB_ENV_SOURCED=1

# strip_quotes <value> — remove a single layer of surrounding double quotes.
strip_quotes() {
  local value="${1:-}"
  value="${value%\"}"
  value="${value#\"}"
  printf '%s' "$value"
}

# load_simple_env_file <file> [SKIP_KEY...]
#   Import `KEY=VALUE` lines from a dotenv-style file into the environment,
#   but only for keys that are not already SET (see Risky contracts above).
#   Any KEY listed after <file> is skipped entirely.
load_simple_env_file() {
  local file="${1:-}"
  [[ -f "$file" ]] || return 0
  shift || true
  local -A _ELB_ENV_SKIP=()
  local k
  for k in "$@"; do _ELB_ENV_SKIP["$k"]=1; done
  local key value
  while IFS='=' read -r key value; do
    [[ -n "${key:-}" ]] || continue
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -z "${_ELB_ENV_SKIP[$key]:-}" ]] || continue
    value="$(strip_quotes "${value:-}")"
    # Set-vs-unset test — preserves explicit empty-string exports.
    if [[ -z "${!key+x}" ]]; then
      export "$key=$value"
    fi
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$file" || true)
}

# _azd_env_file
#   Best-effort path to the active azd environment's `.env` file, used as a
#   fallback when `azd env get-values` is slow/unavailable. Resolution order:
#   $AZURE_ENV_NAME, then .azure/config.json `defaultEnvironment`, then the
#   sole directory under .azure/ when exactly one exists. Echoes nothing when
#   it cannot resolve a real file.
_azd_env_file() {
  local root="${REPO_ROOT:-$PWD}" name=""
  if [[ -n "${AZURE_ENV_NAME:-}" ]]; then
    name="$AZURE_ENV_NAME"
  elif [[ -f "$root/.azure/config.json" ]]; then
    name="$(sed -n 's/.*"defaultEnvironment"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$root/.azure/config.json" | head -n1)"
  fi
  if [[ -z "$name" ]]; then
    local dirs=("$root"/.azure/*/)
    if [[ ${#dirs[@]} -eq 1 && -d "${dirs[0]}" ]]; then
      name="$(basename "${dirs[0]}")"
    fi
  fi
  # NOTE: terminate with an `if` block, NOT a `[[ ... ]] && printf` one-liner.
  # As the function's last command, a one-liner whose test is false returns
  # exit 1, which makes `f="$(_azd_env_file)"` in a `set -Eeuo pipefail`
  # caller (quick-deploy.sh) abort the whole script silently when no azd env
  # exists — exactly the GitHub Actions build-images failure mode. The `if`
  # block returns 0 when the file is absent, so the caller degrades to its
  # empty-fallback path instead of dying.
  if [[ -n "$name" && -f "$root/.azure/$name/.env" ]]; then
    printf '%s' "$root/.azure/$name/.env"
  fi
}

# load_azd_env
#   Import the keys reported by `azd env get-values` into the environment,
#   but only for keys that are not already SET. When the CLI yields nothing
#   (azd absent, not logged in, or killed by the timeout) it falls back to
#   reading the env's `.env` file directly so per-deployment pins survive.
load_azd_env() {
  local values=""
  if command -v azd >/dev/null 2>&1; then
    # Redirect stdin from /dev/null so azd can never block on an interactive
    # prompt (e.g. "Select an environment to use:" when no default env is
    # configured) — it fails fast instead of burning the whole timeout.
    if command -v timeout >/dev/null 2>&1; then
      values="$(timeout 8s azd env get-values </dev/null 2>/dev/null || true)"
    else
      values="$(azd env get-values </dev/null 2>/dev/null || true)"
    fi
  fi
  # Fall back to the on-disk env file when the CLI produced no usable
  # KEY=VALUE data. Testing for an actual assignment line (not merely
  # "non-empty") is essential: a killed interactive prompt leaves the prompt
  # text on stdout, which is non-whitespace but carries no keys.
  if ! grep -qE '^[A-Za-z_][A-Za-z0-9_]*=' <<< "$values"; then
    # `|| true`: defence-in-depth so a non-zero from the resolver can never
    # trip a `set -e` caller even if the function is later changed to return
    # an error code.
    local f
    f="$(_azd_env_file)" || true
    if [[ -n "$f" ]]; then
      values="$(cat "$f" 2>/dev/null || true)"
    fi
  fi
  local key value
  while IFS='=' read -r key value; do
    [[ -n "${key:-}" ]] || continue
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="$(strip_quotes "${value:-}")"
    if [[ -z "${!key+x}" ]]; then
      export "$key=$value"
    fi
  done <<< "$values"
}
