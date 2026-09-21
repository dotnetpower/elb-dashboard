#!/usr/bin/env bash
# Probe Azure capabilities from the deployed API sidecar's managed identity.
#
# The caller authenticates to the dashboard API with a delegated Azure CLI
# token. Each endpoint then performs its Azure work under the Container App's
# UAMI from inside the private VNet, so private Storage and ACR checks do not
# accidentally test the deployer's local identity or public network path.

set -euo pipefail

required=(
  AZURE_SUBSCRIPTION_ID
  AZURE_RESOURCE_GROUP
  API_CLIENT_ID
  CONTAINER_APP_FQDN
  STORAGE_ACCOUNT_NAME
  ACR_NAME
)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "ERROR: required env var $name is not set" >&2
    exit 2
  fi
done

command -v az >/dev/null 2>&1 || { echo "ERROR: az CLI is required" >&2; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required" >&2; exit 2; }

base_url="https://$CONTAINER_APP_FQDN"
if command -v timeout >/dev/null 2>&1; then
  token="$(timeout 30s az account get-access-token \
    --scope "api://$API_CLIENT_ID/.default" \
    --query accessToken -o tsv --only-show-errors 2>/dev/null || true)"
else
  token="$(az account get-access-token \
    --scope "api://$API_CLIENT_ID/.default" \
    --query accessToken -o tsv --only-show-errors 2>/dev/null || true)"
fi
if [[ -z "$token" ]]; then
  echo "ERROR: could not acquire a delegated token for the dashboard API audience" >&2
  exit 1
fi

api_get() {
  local path="$1"
  shift
  curl -fsS --retry 5 --retry-all-errors --retry-delay 3 --max-time 60 \
    --get "$base_url$path" \
    -H "Authorization: Bearer $token" \
    "$@"
}

ready_json="$(api_get /api/health/ready)"
jq -e '.status == "ready"' <<<"$ready_json" >/dev/null \
  || { echo "ERROR: deployed readiness probe failed" >&2; exit 1; }
echo "OK: deployed readiness and Storage Table data plane"

discovery_json="$(api_get /api/health/azure-discovery)"
jq -e '
  .credential.status == "ok" and
  .subscriptions_list.status == "ok" and
  .resource_groups_list.status == "ok"
' <<<"$discovery_json" >/dev/null \
  || { echo "ERROR: deployed Azure discovery probe failed" >&2; exit 1; }
echo "OK: deployed Azure credential and subscription/RG discovery"

storage_json="$(api_get /api/monitor/storage \
  --data-urlencode "subscription_id=$AZURE_SUBSCRIPTION_ID" \
  --data-urlencode "resource_group=$AZURE_RESOURCE_GROUP" \
  --data-urlencode "account_name=$STORAGE_ACCOUNT_NAME")"
jq -e '(.degraded // false) == false and (.name | length) > 0' \
  <<<"$storage_json" >/dev/null \
  || { echo "ERROR: deployed Storage capability probe failed" >&2; exit 1; }
echo "OK: deployed Storage management and container listing"

acr_json="$(api_get /api/monitor/acr \
  --data-urlencode "subscription_id=$AZURE_SUBSCRIPTION_ID" \
  --data-urlencode "resource_group=$AZURE_RESOURCE_GROUP" \
  --data-urlencode "registry_name=$ACR_NAME")"
jq -e '(.degraded // false) == false and (.login_server | length) > 0' \
  <<<"$acr_json" >/dev/null \
  || { echo "ERROR: deployed ACR capability probe failed" >&2; exit 1; }
echo "OK: deployed ACR management and repository listing"

unset token
