#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
out_dir="$repo_root/docs/mock-app"

rm -rf "$out_dir"
mkdir -p "$out_dir"

cd "$repo_root/web"
VITE_DOCS_MOCK_PREVIEW=true \
VITE_AUTH_DEV_BYPASS=true \
VITE_FEATURE_CUSTOM_DB=true \
VITE_FEATURE_LAB_TOOLS=true \
VITE_FEATURE_TERMINAL=false \
npm run build -- --base /elb-dashboard/mock-app/ --outDir ../docs/mock-app