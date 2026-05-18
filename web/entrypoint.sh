#!/bin/sh
set -eu

python3 - <<'PY'
import json
import os
from pathlib import Path

keys = [
    "VITE_API_BASE_URL",
    "VITE_AUTH_DEV_BYPASS",
    "VITE_AZURE_REDIRECT_URI",
    "VITE_AZURE_TENANT_ID",
    "VITE_AZURE_CLIENT_ID",
]
config = {key: os.environ.get(key, "") for key in keys}
if not config["VITE_AZURE_CLIENT_ID"]:
    config["VITE_AZURE_CLIENT_ID"] = os.environ.get("API_CLIENT_ID", "")
if not config["VITE_AZURE_TENANT_ID"]:
    config["VITE_AZURE_TENANT_ID"] = os.environ.get("AZURE_TENANT_ID", "common")
if not config["VITE_AZURE_REDIRECT_URI"]:
    config["VITE_AZURE_REDIRECT_URI"] = "__RUNTIME__"
if not config["VITE_AUTH_DEV_BYPASS"]:
    config["VITE_AUTH_DEV_BYPASS"] = "false"

payload = json.dumps(config, separators=(",", ":"))
Path("/usr/share/nginx/html/runtime-config.js").write_text(
    f"window.__ELB_RUNTIME_CONFIG__ = {payload};\n",
    encoding="utf-8",
)
PY

exec "$@"
