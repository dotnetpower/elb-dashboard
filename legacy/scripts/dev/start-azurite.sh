#!/usr/bin/env bash
# Run Azurite (Azure Storage emulator) in Docker for local Durable Functions.
# Idempotent: reuses an existing container if present.
set -euo pipefail

NAME="${1:-azurite-elb}"
DATA_DIR="${2:-$HOME/.azurite}"

mkdir -p "$DATA_DIR"

if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "==> Azurite already running ($NAME)"
elif docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "==> Starting existing Azurite container ($NAME)..."
  docker start "$NAME" >/dev/null
else
  echo "==> Pulling and starting Azurite ($NAME)..."
  docker run -d --restart unless-stopped \
    --name "$NAME" \
    -p 10000:10000 -p 10001:10001 -p 10002:10002 \
    -v "$DATA_DIR:/data" \
    mcr.microsoft.com/azure-storage/azurite:latest \
    azurite \
      --blobHost 0.0.0.0 \
      --queueHost 0.0.0.0 \
      --tableHost 0.0.0.0 \
      --location /data \
      --skipApiVersionCheck >/dev/null
fi

# Wait for Blob endpoint
echo -n "==> Waiting for Azurite (Blob 10000)"
for _ in $(seq 1 30); do
  if (echo > /dev/tcp/127.0.0.1/10000) >/dev/null 2>&1; then
    echo " ready"
    exit 0
  fi
  echo -n "."
  sleep 1
done
echo
echo "ERROR: Azurite did not become ready on port 10000." >&2
exit 1
