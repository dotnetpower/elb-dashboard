#!/usr/bin/env bash
# One-shot end-to-end local bootstrap: App Registration + Key Vault + secret.
# After this finishes, `func start` from api/ should work, and the SPA
# at http://localhost:8090 will be able to call the API.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
"$repo_root/scripts/dev/setup-app-registration.sh"
"$repo_root/scripts/dev/setup-keyvault.sh"
"$repo_root/scripts/dev/generate-client-secret.sh"

cat <<EOF

============================================================
 Local bootstrap complete.
 Next:
   cd api && func start         # Function App on http://localhost:7071
   cd web && npm run dev        # SPA on http://localhost:8090
============================================================
EOF
