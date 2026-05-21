#!/usr/bin/env bash
# Print consistent azd up progress markers for hooks and wrapper scripts.

set -euo pipefail

cmd="${1:-plan}"
shift || true

timestamp() {
  date -u +%H:%M:%S
}

case "$cmd" in
  plan)
    cat <<'EOF'

============================================================
azd up progress map
============================================================
[0/7] Local bootstrap        - ./deploy.sh only: login, azd env, env values
[1/7] Provider registration  - preprovision: required Azure providers
[2/7] Bicep provision        - azd: RG, VNet, identity, ACR, Storage, Key Vault, Container Apps Environment, bootstrap app
[3/7] App registration       - postprovision: create/reuse SPA/API App Registration when needed
[4/7] Resource validation    - postprovision: Storage HNS and workspace tags
[5/7] Image builds           - postprovision: api, frontend, terminal via az acr build
[6/7] Sidecar swap           - postprovision: replace bootstrap app with six-sidecar layout
[7/7] Health check           - postprovision: wait for /api/health and print URL
============================================================

EOF
    ;;
  step)
    number="${1:?step number required}"
    title="${2:?step title required}"
    detail="${3:-}"
    printf '[%s] [%s/7] %s\n' "$(timestamp)" "$number" "$title"
    if [[ -n "$detail" ]]; then
      printf '          %s\n' "$detail"
    fi
    ;;
  done)
    number="${1:?step number required}"
    title="${2:?step title required}"
    printf '[%s] [%s/7] done: %s\n' "$(timestamp)" "$number" "$title"
    ;;
  note)
    message="${1:?message required}"
    printf '[%s]      %s\n' "$(timestamp)" "$message"
    ;;
  *)
    echo "ERROR: unknown progress command: $cmd" >&2
    exit 2
    ;;
esac