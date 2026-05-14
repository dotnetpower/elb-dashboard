#!/usr/bin/env bash
# Generate a client secret for the App Registration used by the API and
# write it into api/local.settings.json. Required for the OBO flow.
#
# Usage:
#   scripts/dev/generate-client-secret.sh
#
# Reads the appId from api/local.settings.json (API_CLIENT_ID).
# Existing secrets with the same display name are deleted and recreated.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
api_settings="$repo_root/api/local.settings.json"
DISPLAY_NAME="${1:-dev-secret}"
YEARS="${2:-1}"

if [[ ! -f "$api_settings" ]]; then
  echo "ERROR: $api_settings not found. Run setup-app-registration.sh first." >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required (sudo apt install jq)." >&2
  exit 1
fi

app_id="$(jq -r '.Values.API_CLIENT_ID' "$api_settings")"
if [[ -z "$app_id" || "$app_id" == "null" ]]; then
  echo "ERROR: API_CLIENT_ID missing in $api_settings." >&2
  exit 1
fi

echo "==> App ID: $app_id"
echo "==> Generating client secret '$DISPLAY_NAME' (valid $YEARS year)..."
secret="$(az ad app credential reset \
  --id "$app_id" \
  --append \
  --display-name "$DISPLAY_NAME" \
  --years "$YEARS" \
  --query password -o tsv)"

if [[ -z "$secret" ]]; then
  echo "ERROR: failed to create client secret." >&2
  exit 1
fi

tmp="$(mktemp)"
jq --arg s "$secret" '.Values.API_CLIENT_SECRET = $s' "$api_settings" > "$tmp"
mv "$tmp" "$api_settings"

echo "==> Wrote API_CLIENT_SECRET to $api_settings"
echo
echo "Reminder: this secret is in plaintext on disk. Do not commit local.settings.json."
