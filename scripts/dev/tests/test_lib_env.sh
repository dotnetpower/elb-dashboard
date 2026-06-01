#!/usr/bin/env bash
# Regression test for scripts/dev/lib-env.sh.
#
# Guards the set-vs-unset contract that caused the 2026-05-21 / 2026-05-25
# frontend env-leak incidents: an explicit empty-string export MUST survive
# load_simple_env_file / load_azd_env (i.e. the guard is `${!key+x}`, not
# `${!key:-}`).
#
# Run: bash scripts/dev/tests/test_lib_env.sh   (exit 0 = pass)

set -Eeuo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$THIS_DIR/../lib-env.sh"

fail() { printf '\033[31mFAIL:\033[0m %s\n' "$*" >&2; exit 1; }
pass() { printf '\033[32mok:\033[0m %s\n' "$*"; }

# shellcheck source=scripts/dev/lib-env.sh
. "$LIB"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<'EOF'
# comment line ignored
VITE_API_BASE_URL="http://localhost:8085"
VITE_AUTH_DEV_BYPASS=true
PLAIN_UNSET=from_file
SKIP_ME=from_file
not a valid line
EOF

# 1. An explicit empty-string export must NOT be overwritten by the file.
export VITE_API_BASE_URL=""
# 2. A var that is genuinely unset must be filled from the file.
unset PLAIN_UNSET || true
# 3. A pre-set non-empty value must NOT be overwritten.
export VITE_AUTH_DEV_BYPASS=false
# 4. A SKIP key must never be imported even though it is unset.
unset SKIP_ME || true

load_simple_env_file "$tmp" SKIP_ME

[[ "${VITE_API_BASE_URL}" == "" ]] \
  || fail "empty export overwritten -> '${VITE_API_BASE_URL}' (regression to \${!key:-})"
pass "explicit empty-string export preserved"

[[ "${PLAIN_UNSET:-__missing__}" == "from_file" ]] \
  || fail "unset var not imported -> '${PLAIN_UNSET:-__missing__}'"
pass "unset var imported from file"

[[ "${VITE_AUTH_DEV_BYPASS}" == "false" ]] \
  || fail "pre-set value overwritten -> '${VITE_AUTH_DEV_BYPASS}'"
pass "pre-set non-empty value preserved"

[[ -z "${SKIP_ME+x}" ]] \
  || fail "SKIP key imported despite skip list -> '${SKIP_ME:-}'"
pass "skip list honoured"

# strip_quotes sanity
[[ "$(strip_quotes '"quoted"')" == "quoted" ]] || fail "strip_quotes broke"
pass "strip_quotes removes one layer of double quotes"

printf '\033[32mALL PASS\033[0m\n'
