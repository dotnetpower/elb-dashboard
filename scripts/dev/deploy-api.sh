#!/usr/bin/env bash
# deploy-api.sh — One-command backend deployment for elb-dashboard.
#
# Usage:  ./scripts/dev/deploy-api.sh
#
# This replaces the broken `azd deploy api` path.  azd does not include
# .python_packages in its zip, so the Function App fails to start.  This script
# zips everything locally (including pre-installed dependencies) and uploads it
# as a SAS-protected blob that the Function App runs from.
set -euo pipefail

FUNC_APP="${FUNC_APP:-func-prod2-fuxqeza73ska4}"
RG="${RG:-rg-prod2}"
STORAGE_ACCT="${STORAGE_ACCT:-stelbfuxqeza73ska4}"
CONTAINER="function-releases"
BLOB="funcapp-$(date +%Y%m%d%H%M).zip"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_DIR="$(cd "$SCRIPT_DIR/../../api" && pwd)"

echo "==> Ensuring .python_packages is up to date..."
# Azure Functions runtime is Python 3.11. We MUST install packages with a
# Python 3.11 interpreter so any compiled C extensions (e.g. _cffi_backend)
# match the runtime ABI. Prefer the workspace venv when available; fall back to
# system python3.11 / python3 with a stern warning.
PY_BIN=""
for candidate in \
  "$SCRIPT_DIR/../../.venv/bin/python3.11" \
  "$SCRIPT_DIR/../../.venv/bin/python" \
  "$(command -v python3.11 || true)" \
  "$(command -v python3 || true)"; do
  if [[ -x "$candidate" ]]; then
    ver=$("$candidate" -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))' 2>/dev/null || echo "")
    if [[ "$ver" == "3.11" ]]; then
      PY_BIN="$candidate"
      break
    fi
  fi
done
if [[ -z "$PY_BIN" ]]; then
  echo "✗ No Python 3.11 interpreter found. Azure Functions requires 3.11." >&2
  echo "  Create one with: python3.11 -m venv .venv && source .venv/bin/activate && pip install -r api/requirements.txt" >&2
  exit 1
fi
echo "    using interpreter: $PY_BIN ($("$PY_BIN" --version 2>&1))"
# Force-reinstall site-packages from scratch so stale 3.10 binaries are evicted.
SITE_PKGS="$API_DIR/.python_packages/lib/site-packages"
rm -rf "$SITE_PKGS"
mkdir -p "$SITE_PKGS"
"$PY_BIN" -m pip install -r "$API_DIR/requirements.txt" \
  --target "$SITE_PKGS" -q 2>&1 | tail -5

echo "==> Packaging $API_DIR → /tmp/$BLOB..."
if command -v zip >/dev/null 2>&1; then
  (cd "$API_DIR" && zip -r "/tmp/$BLOB" . \
    -x '__pycache__/*' '*.pyc' 'local.settings.json' '.venv/*' 'tests/*') \
    > /dev/null
else
  # Fallback: use Python's zipfile module when the `zip` package is unavailable.
  python3 - "$API_DIR" "/tmp/$BLOB" <<'PY'
import os, sys, zipfile, fnmatch
src, out = sys.argv[1], sys.argv[2]
exclude_globs = ['__pycache__', '*.pyc', 'local.settings.json', '.venv', 'tests']
def excluded(rel):
    parts = rel.split(os.sep)
    for pat in exclude_globs:
        for p in parts:
            if fnmatch.fnmatch(p, pat):
                return True
    return False
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not excluded(os.path.relpath(os.path.join(root, d), src))]
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src)
            if excluded(rel):
                continue
            zf.write(full, rel)
print(f"Wrote {out} ({os.path.getsize(out)} bytes)")
PY
fi

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
