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

# load_azd_env
#   Import the keys reported by `azd env get-values` into the environment,
#   but only for keys that are not already SET. No-op when azd is absent.
load_azd_env() {
  command -v azd >/dev/null 2>&1 || return 0
  local values
  if command -v timeout >/dev/null 2>&1; then
    values="$(timeout 8s azd env get-values 2>/dev/null || true)"
  else
    values="$(azd env get-values 2>/dev/null || true)"
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
