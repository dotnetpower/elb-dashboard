#!/usr/bin/env bash
# setup-gha-oidc.sh — one-shot setup for GitHub Actions → Azure OIDC.
#
# Creates (idempotently):
#   1. An Azure AD App Registration + service principal for GitHub Actions
#   2. Federated identity credentials so the App accepts tokens from this
#      repo on (a) push to main, (b) pull_request, (c) "production"
#      environment deployments
#   3. RBAC role assignments at the minimum required scope:
#        - Contributor      on the ACR (image builds + import + ACR Tasks
#                           `listBuildSourceUploadUrl/action` — `AcrPush`
#                           alone does not cover `az acr build`'s context
#                           upload step)
#        - Contributor      on the Container App (revision swap)
#        - Reader           on the resource group (so quick-deploy.sh
#                           can `az containerapp show`, `az acr show`, etc.)
#
# No client secrets are created. The GitHub workflow authenticates via OIDC
# federated credential — see charter §12.
#
# Prints the three values you must paste into GitHub:
#   Settings → Secrets and variables → Actions
#     Secrets:   AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID
#     Variables: ACR_NAME, ACR_LOGIN_SERVER, AZURE_RESOURCE_GROUP,
#                CONTAINER_APP_NAME, CONTAINER_APP_FQDN, API_CLIENT_ID
#
# Usage:
#   scripts/dev/setup-gha-oidc.sh                  # uses azd env defaults
#   GITHUB_REPO=owner/repo scripts/dev/setup-gha-oidc.sh
#
# Requires: az CLI ≥ 2.81 logged in with Owner or User Access Administrator
# on the target subscription (to create role assignments).

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

ts() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

GITHUB_REPO="${GITHUB_REPO:-dotnetpower/elb-dashboard}"
APP_NAME="${APP_NAME:-gha-elb-dashboard}"

# ---------------------------------------------------------------------------
# Discover Azure resource names from azd env (preferred) or env vars.
# load_azd_env only fills keys that are currently UNSET (set-vs-unset guard),
# so an explicit empty-string export is preserved — see lib-env.sh.
# ---------------------------------------------------------------------------
. "$REPO_ROOT/scripts/dev/lib-env.sh"
load_azd_env

: "${AZURE_SUBSCRIPTION_ID:?AZURE_SUBSCRIPTION_ID not set (run 'azd env refresh' or export it)}"
: "${AZURE_TENANT_ID:?AZURE_TENANT_ID not set}"
: "${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP not set}"
: "${ACR_NAME:?ACR_NAME not set}"
: "${CONTAINER_APP_NAME:?CONTAINER_APP_NAME not set}"

ts "Target subscription : $AZURE_SUBSCRIPTION_ID"
ts "Target tenant       : $AZURE_TENANT_ID"
ts "Target resource grp : $AZURE_RESOURCE_GROUP"
ts "Target ACR          : $ACR_NAME"
ts "Target Container App: $CONTAINER_APP_NAME"
ts "GitHub repo         : $GITHUB_REPO"
ts "App Registration    : $APP_NAME"
echo ""

# Align az CLI to the target subscription so `az acr show` / `az containerapp
# show` / `az role assignment` all hit the right place. azd env's
# AZURE_SUBSCRIPTION_ID is authoritative; an unaligned `az login` profile
# (e.g. a different demo sub set as the default) would otherwise fail with
# "resource not found" at the RBAC step.
current_sub="$(az account show --query id -o tsv 2>/dev/null || true)"
if [[ "$current_sub" != "$AZURE_SUBSCRIPTION_ID" ]]; then
  ts "==> Switching active az subscription: $current_sub -> $AZURE_SUBSCRIPTION_ID"
  az account set --subscription "$AZURE_SUBSCRIPTION_ID"
fi

# ---------------------------------------------------------------------------
# 1. App Registration + service principal
# ---------------------------------------------------------------------------
ts "==> Ensuring App Registration '$APP_NAME'"
APP_ID="$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv 2>/dev/null || true)"
if [[ -z "$APP_ID" ]]; then
  ts "    Creating new App Registration"
  APP_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"
else
  ts "    Reusing existing App Registration ($APP_ID)"
fi

SP_ID="$(az ad sp list --filter "appId eq '$APP_ID'" --query "[0].id" -o tsv 2>/dev/null || true)"
if [[ -z "$SP_ID" ]]; then
  ts "    Creating service principal for $APP_ID"
  SP_ID="$(az ad sp create --id "$APP_ID" --query id -o tsv)"
else
  ts "    Reusing existing service principal ($SP_ID)"
fi

# ---------------------------------------------------------------------------
# 2. Federated identity credentials
# ---------------------------------------------------------------------------
ensure_federated_credential() {
  local name="$1" subject="$2"
  local existing
  existing="$(az ad app federated-credential list --id "$APP_ID" --query "[?name=='$name'].name" -o tsv 2>/dev/null || true)"
  if [[ -n "$existing" ]]; then
    ts "    Federated credential '$name' already present"
    return 0
  fi
  ts "    Creating federated credential '$name' → $subject"
  az ad app federated-credential create --id "$APP_ID" --parameters "$(cat <<JSON
{
  "name": "$name",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "$subject",
  "audiences": ["api://AzureADTokenExchange"]
}
JSON
)" -o none
}

ts "==> Ensuring federated identity credentials"
ensure_federated_credential "gha-main"       "repo:${GITHUB_REPO}:ref:refs/heads/main"
ensure_federated_credential "gha-pr"         "repo:${GITHUB_REPO}:pull_request"
ensure_federated_credential "gha-production" "repo:${GITHUB_REPO}:environment:production"

# ---------------------------------------------------------------------------
# 3. RBAC at minimum scope
# ---------------------------------------------------------------------------
ACR_ID="$(az acr show --name "$ACR_NAME" --query id -o tsv)"
CA_ID="$(az containerapp show --name "$CONTAINER_APP_NAME" --resource-group "$AZURE_RESOURCE_GROUP" --query id -o tsv)"
RG_ID="/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${AZURE_RESOURCE_GROUP}"

ensure_role_assignment() {
  local role="$1" scope="$2"
  local existing
  existing="$(az role assignment list --assignee "$APP_ID" --role "$role" --scope "$scope" --query "[0].id" -o tsv 2>/dev/null || true)"
  if [[ -n "$existing" ]]; then
    ts "    Role '$role' already assigned at ${scope##*/}"
    return 0
  fi
  ts "    Assigning '$role' on ${scope##*/}"
  # Role assignment propagation can take up to ~5 minutes; the workflow
  # doesn't care because it runs later, not now.
  az role assignment create --assignee "$APP_ID" --role "$role" --scope "$scope" -o none
}

ts "==> Ensuring RBAC at minimum scope"
ensure_role_assignment "Contributor"  "$ACR_ID"
ensure_role_assignment "Contributor"  "$CA_ID"
ensure_role_assignment "Reader"       "$RG_ID"

ACR_LOGIN_SERVER="${ACR_LOGIN_SERVER:-$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)}"
CONTAINER_APP_FQDN="${CONTAINER_APP_FQDN:-$(az containerapp show --name "$CONTAINER_APP_NAME" --resource-group "$AZURE_RESOURCE_GROUP" --query "properties.configuration.ingress.fqdn" -o tsv)}"

# ---------------------------------------------------------------------------
# Output: what to paste into GitHub
# ---------------------------------------------------------------------------
cat <<EOF

============================================================
GitHub Actions OIDC setup complete.
============================================================

Paste these into  https://github.com/${GITHUB_REPO}/settings/secrets/actions

  ── Secrets ────────────────────────────────────────────
    AZURE_CLIENT_ID       = ${APP_ID}
    AZURE_TENANT_ID       = ${AZURE_TENANT_ID}
    AZURE_SUBSCRIPTION_ID = ${AZURE_SUBSCRIPTION_ID}

Paste these into  https://github.com/${GITHUB_REPO}/settings/variables/actions

  ── Variables ──────────────────────────────────────────
    ACR_NAME             = ${ACR_NAME}
    ACR_LOGIN_SERVER     = ${ACR_LOGIN_SERVER}
    AZURE_RESOURCE_GROUP = ${AZURE_RESOURCE_GROUP}
    CONTAINER_APP_NAME   = ${CONTAINER_APP_NAME}
    CONTAINER_APP_FQDN   = ${CONTAINER_APP_FQDN}
    API_CLIENT_ID        = ${API_CLIENT_ID:-<set from your SPA App Registration>}

Then create the 'production' environment:
  https://github.com/${GITHUB_REPO}/settings/environments
    → New environment → name: production
    → Required reviewers: add yourself
    (deploy.yml gates 'az containerapp update' behind this approval)

Verification:
  1. Push any change under api/, web/, or terminal/ to main
     → 'Build Images' workflow runs, produces gha-<sha> + latest-main tags.
  2. Actions tab → 'Deploy to Container App' → Run workflow
     → choose sidecar='all', tag='latest-main' → approve → revision swap.

EOF
