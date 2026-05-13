#!/usr/bin/env bash
# deploy-api.sh — One-command backend deployment for elastic-blast-azure-functionapp.
#
# Usage:  ./scripts/dev/deploy-api.sh
#
# This replaces the broken `azd deploy api` path.  azd does not include
# .python_packages in its zip, so the Function App fails to start.  This script
# zips everything locally (including pre-installed dependencies) and uploads it
# as a SAS-protected blob that the Function App runs from.
set -euo pipefail

FUNC_APP="func-elb-prod-ga5754pr7jw3u"
RG="rg-elb-prod"
STORAGE_ACCT="stelbga5754pr7jw3u"
CONTAINER="function-releases"
BLOB="funcapp-$(date +%Y%m%d%H%M).zip"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_DIR="$(cd "$SCRIPT_DIR/../../api" && pwd)"

echo "==> Ensuring .python_packages is up to date..."
pip install -r "$API_DIR/requirements.txt" \
  --target "$API_DIR/.python_packages/lib/site-packages" -q 2>&1 | tail -3

echo "==> Packaging $API_DIR → /tmp/$BLOB..."
(cd "$API_DIR" && zip -r "/tmp/$BLOB" . \
  -x '__pycache__/*' '*.pyc' 'local.settings.json' '.venv/*' 'tests/*') \
  > /dev/null

echo "==> Uploading to $STORAGE_ACCT/$CONTAINER/$BLOB..."
az storage blob upload \
  --account-name "$STORAGE_ACCT" \
  --container-name "$CONTAINER" \
  --name "$BLOB" \
  --file "/tmp/$BLOB" \
  --overwrite \
  --auth-mode login \
  --output none

EXPIRY=$(date -u -d "+7 days" +%Y-%m-%dT%H:%MZ)
SAS_URL=$(az storage blob generate-sas \
  --account-name "$STORAGE_ACCT" \
  --container-name "$CONTAINER" \
  --name "$BLOB" \
  --permissions r \
  --expiry "$EXPIRY" \
  --auth-mode login \
  --as-user \
  --full-uri \
  -o tsv)

echo "==> Setting WEBSITE_RUN_FROM_PACKAGE..."
az functionapp config appsettings set \
  -n "$FUNC_APP" -g "$RG" \
  --settings "WEBSITE_RUN_FROM_PACKAGE=$SAS_URL" \
  --output none

echo "==> Restarting $FUNC_APP..."
az functionapp restart -n "$FUNC_APP" -g "$RG" --output none

echo "==> Waiting 15s for cold start..."
sleep 15

STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://$FUNC_APP.azurewebsites.net/api/health" 2>/dev/null || echo "000")
if [ "$STATUS" = "200" ]; then
  echo "✓ API healthy ($STATUS)"
else
  echo "⚠ API returned $STATUS — may still be starting. Retry in 30s."
fi

echo "==> Done. Blob: $BLOB"
