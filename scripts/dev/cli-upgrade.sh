#!/usr/bin/env bash
# cli-upgrade.sh — safe CLI rolling-update for the deployed Container App.
#
# Wraps quick-deploy.sh / postprovision.sh with a snapshot + health-check
# + auto-rollback envelope so an operator can run `git pull` + build +
# rolling-update from a workstation without losing the previous revision
# if the new image is unhealthy.
#
# Preferred path for non-emergency upgrades is still the in-browser
# In-app Upgrade flow (see docs/user-guide/upgrades.md). Use this CLI
# only when:
#   - the In-app upgrade is disabled (UPGRADE_GIT_REMOTE unset), OR
#   - sidecar layout / Bicep / terminal-base-image actually changed
#     and you need the full postprovision template swap, OR
#   - you need to roll back from a workstation when the SPA is down.
#
# Usage:
#   scripts/dev/cli-upgrade.sh <scope> [flags]
#
# Scopes:
#   api           Build elb-api → patch api+worker+beat (also picks up
#                 worker task changes — see quick-deploy.sh).
#   frontend      Build elb-frontend → patch frontend with env vars.
#   terminal      Build elb-terminal → patch terminal.
#   full          Build all 3 images + run postprovision.sh (template
#                 swap). Required when sidecar shape changed.
#   rollback      Restore per-sidecar image refs from the snapshot taken
#                 by the most recent upgrade run.
#
# Flags:
#   --pull               git pull --ff-only before building. Refuses if
#                        the working tree is dirty (use --allow-dirty
#                        to override). Default: do NOT pull.
#   --allow-dirty        Skip the dirty-tree refusal (you accept the
#                        risk of building with uncommitted edits).
#   --branch <name>      After --pull, must be on this branch. Default:
#                        whatever branch HEAD already points at.
#   --tag <tag>          Override the timestamp tag for ACR build.
#   --health-timeout <s> Seconds to wait for /api/health/ready=200. Default: 180.
#   --no-auto-rollback   On health-check failure, DO NOT auto-rollback.
#                        Print the rollback command instead.
#   --skip-parity-check  Skip the Storage isolation parity preflight that
#                        rejects deploys when workload Storage is set to
#                        publicNetworkAccess=Disabled while no Private
#                        Endpoint exists (the workload could not reach
#                        Storage at all). Use only when you know what
#                        you're doing.
#   --yes                Skip the interactive "proceed?" prompt.
#   --dry-run            Print the plan, do not build or PATCH.
#   --logs               Tail the api sidecar logs after a successful
#                        deploy. Implies --yes.
#   --help               This message.
#
# Required env (loaded from azd env automatically when not exported):
#   AZURE_RESOURCE_GROUP   AZURE_SUBSCRIPTION_ID (optional)
#   ACR_NAME               ACR_LOGIN_SERVER
#   CONTAINER_APP_NAME     CONTAINER_APP_FQDN
#
# Examples:
#   scripts/dev/cli-upgrade.sh api --pull --yes
#   scripts/dev/cli-upgrade.sh frontend --tag rc-2026-05-23
#   scripts/dev/cli-upgrade.sh full --no-auto-rollback
#   scripts/dev/cli-upgrade.sh rollback --yes

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

ts() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
warn() { printf '\033[33mWARN:\033[0m %s\n' "$*" >&2; }

usage() {
  sed -n '2,55p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# Flag parsing.
# ---------------------------------------------------------------------------
SCOPE=""
DO_PULL=false
ALLOW_DIRTY=false
TARGET_BRANCH=""
TAG=""
HEALTH_TIMEOUT=180
AUTO_ROLLBACK=true
SKIP_PARITY=false
ASSUME_YES=false
DRY_RUN=false
TAIL_LOGS=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    api|worker|beat|frontend|terminal|full|rollback) SCOPE="$1" ;;
    --pull)              DO_PULL=true ;;
    --allow-dirty)       ALLOW_DIRTY=true ;;
    --branch)            shift; TARGET_BRANCH="${1:-}" ;;
    --tag)               shift; TAG="${1:-}" ;;
    --health-timeout)    shift; HEALTH_TIMEOUT="${1:-180}" ;;
    --no-auto-rollback)  AUTO_ROLLBACK=false ;;
    --skip-parity-check) SKIP_PARITY=true ;;
    --yes|-y)            ASSUME_YES=true ;;
    --dry-run)           DRY_RUN=true ;;
    --logs)              TAIL_LOGS=true; ASSUME_YES=true ;;
    --help|-h)           usage 0 ;;
    *)                   die "unknown argument: $1 (use --help)" ;;
  esac
  shift
done
[[ -n "$SCOPE" ]] || { usage 2; }
# Map worker/beat to api scope (same image, same deploy path).
case "$SCOPE" in worker|beat) SCOPE="api" ;; esac

# ---------------------------------------------------------------------------
# Env loading (mirrors quick-deploy.sh load_simple_env_file + load_azd_env).
# ---------------------------------------------------------------------------
strip_quotes() { local v="${1:-}"; v="${v%\"}"; v="${v#\"}"; printf '%s' "$v"; }
load_simple_env_file() {
  local file="${1:-}"; [[ -f "$file" ]] || return 0
  while IFS='=' read -r key value; do
    [[ -n "${key:-}" ]] || continue
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="$(strip_quotes "${value:-}")"
    if [[ -z "${!key:-}" ]]; then export "$key=$value"; fi
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$file" || true)
}
load_azd_env() {
  command -v azd >/dev/null 2>&1 || return 0
  local values; values="$(azd env get-values 2>/dev/null || true)"
  while IFS='=' read -r key value; do
    [[ -n "${key:-}" ]] || continue
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="$(strip_quotes "${value:-}")"
    if [[ -z "${!key:-}" ]]; then export "$key=$value"; fi
  done <<< "$values"
}

load_simple_env_file "$REPO_ROOT/.env"
load_simple_env_file "$REPO_ROOT/.env.local"
if [[ -z "${AZURE_RESOURCE_GROUP:-}" || -z "${CONTAINER_APP_NAME:-}" ]]; then
  load_azd_env
fi
for v in AZURE_RESOURCE_GROUP ACR_NAME ACR_LOGIN_SERVER CONTAINER_APP_NAME CONTAINER_APP_FQDN; do
  [[ -n "${!v:-}" ]] || die "$v is unset. Run \`azd env refresh\` or export it."
done

# ---------------------------------------------------------------------------
# Preflight: az login + active subscription.
# ---------------------------------------------------------------------------
if ! az account show -o none >/dev/null 2>&1; then
  die "Not logged in to Azure CLI. Run 'az login' first."
fi
if [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID" >/dev/null
fi

# ---------------------------------------------------------------------------
# Preflight: Storage isolation parity check.
#
# Catches the silent failure mode where someone ran
# `storage-public-access.sh off` (or local-run.sh storage-off/auth-off) in a
# prior local-debug session, set publicNetworkAccess=Disabled, but the
# deployment was never run with LOCKDOWN_PRIVATE_NETWORKING=true — so no
# Private Endpoint exists for Storage. In that state the Container App has
# no network path to Storage data plane and every Table/Blob call returns
# `403 AuthorizationFailure` (Azure's misleading error code for network
# policy block on a public endpoint hit).
#
# Design notes:
#   * Skipped for `rollback` scope — rollback might be the very recovery
#     path FROM a broken-Storage state and must never be blocked here.
#   * Prefers `AZURE_TABLE_ENDPOINT` (the env var the api sidecar actually
#     uses) to derive the workload account name. Falls back to the first
#     `st*` account in the RG only when the env is missing — that fallback
#     could pick a sibling diagnostic account, so it warns loudly.
#   * Uses the storage account's *own* view of approved Private Endpoint
#     connections (via `az storage account show ... privateEndpointConnections`)
#     so hub-and-spoke layouts (PE in a different RG) are counted correctly.
#   * Distinguishes "RBAC failure on the az calls" from "0 PEs". The former
#     warns and skips (preflight is best-effort, not authoritative for RBAC
#     coverage); the latter rejects with a recovery guide.
# ---------------------------------------------------------------------------
preflight_storage_parity() {
  if $SKIP_PARITY; then
    warn "skipping Storage isolation parity check (--skip-parity-check)"
    return 0
  fi
  if [[ "$SCOPE" == "rollback" ]]; then
    # Rollback might be the recovery path FROM a broken-Storage state.
    # Never block a rollback on this check.
    return 0
  fi

  local acct=""
  if [[ -n "${AZURE_TABLE_ENDPOINT:-}" ]]; then
    # https://stelbdashboardmul5oh5j44.table.core.windows.net → stelbdashboardmul5oh5j44
    acct="$(printf '%s' "$AZURE_TABLE_ENDPOINT" \
      | sed -E 's|^https?://([^.]+)\..*|\1|')"
  fi
  if [[ -z "$acct" && -n "${STORAGE_ACCOUNT_NAME:-}" ]]; then
    # azd env exposes the workload account name directly under this key.
    acct="$STORAGE_ACCOUNT_NAME"
  fi
  if [[ -z "$acct" ]]; then
    warn "neither AZURE_TABLE_ENDPOINT nor STORAGE_ACCOUNT_NAME in env;"
    warn "  falling back to first 'st*' account in $AZURE_RESOURCE_GROUP (may pick a diagnostic account)"
    if ! acct="$(az storage account list -g "$AZURE_RESOURCE_GROUP" \
          --query "[?starts_with(name,'st')].name" -o tsv 2>/dev/null)"; then
      warn "cannot list storage accounts in $AZURE_RESOURCE_GROUP (RBAC?); skipping parity check"
      return 0
    fi
    acct="$(printf '%s\n' "$acct" | head -1)"
  fi
  if [[ -z "$acct" ]]; then
    warn "no workload Storage account resolved — skipping parity check"
    return 0
  fi

  # The storage account's own view of its PEs covers hub-and-spoke (PE in
  # a different RG). RBAC needed: Reader on the storage account.
  local show_json
  if ! show_json="$(az storage account show -n "$acct" \
        --query '{public:publicNetworkAccess, pecs:privateEndpointConnections[?properties.privateLinkServiceConnectionState.status==`Approved`]}' \
        -o json 2>&1)"; then
    warn "cannot read storage account '$acct' (RBAC on the account?); skipping parity check"
    warn "  az error: ${show_json:0:200}"
    return 0
  fi
  local public pe_count
  # Parse both fields in one jq invocation (1 fork instead of 2).
  read -r public pe_count < <(printf '%s' "$show_json" \
    | jq -r '"\(.public) \(.pecs | length)"')
  ts "==> Storage parity: account=$acct publicNetworkAccess=$public approvedPrivateEndpoints=$pe_count"
  if [[ "$public" == "Disabled" && "$pe_count" -eq 0 ]]; then
    cat >&2 <<EOF

ERROR: workload Storage '$acct' is unreachable from the Container App.
       publicNetworkAccess=Disabled AND no approved Private Endpoint
       connection exists on the account (checked via the account's own
       privateEndpointConnections view, so this is correct even in a
       hub-and-spoke layout).

Recovery options:
  (A) Quick reopen (test / local-debug):
      scripts/dev/storage-public-access.sh on --account $acct --rg $AZURE_RESOURCE_GROUP

  (B) Proper production posture (creates the PEs via Bicep):
      azd env set LOCKDOWN_PRIVATE_NETWORKING true && azd provision

Override and deploy anyway (workload will keep failing on Storage):
  --skip-parity-check
EOF
    return 1
  fi
  return 0
}
preflight_storage_parity || die "Storage parity preflight failed (see above)."

SNAPSHOT="${ELB_UPGRADE_SNAPSHOT:-/tmp/elb-upgrade-snapshot-${CONTAINER_APP_NAME}.json}"

# ---------------------------------------------------------------------------
# Helper: snapshot current per-sidecar image refs + active revision.
# ---------------------------------------------------------------------------
take_snapshot() {
  local outfile="$1"
  if $DRY_RUN; then
    ts "==> (dry-run) skipping snapshot of $CONTAINER_APP_NAME"
    return 0
  fi
  ts "==> Snapshotting current revision + image refs → $outfile"
  local json
  json="$(az containerapp show \
    --name "$CONTAINER_APP_NAME" --resource-group "$AZURE_RESOURCE_GROUP" \
    --query '{revision: properties.latestRevisionName, images: properties.template.containers[].{name:name, image:image}}' \
    -o json)" || die "az containerapp show failed (RBAC or wrong RG?)"
  printf '%s\n' "$json" > "$outfile"
  ts "    captured revision: $(jq -r .revision "$outfile" 2>/dev/null || sed -n 's/.*"revision": *"\([^"]*\)".*/\1/p' "$outfile")"
  if command -v jq >/dev/null 2>&1; then
    jq -r '.images[] | "    " + .name + " → " + .image' "$outfile"
  fi
}

# ---------------------------------------------------------------------------
# Helper: poll /api/health/ready for up to $HEALTH_TIMEOUT seconds.
# Returns 0 on 200, non-zero on timeout. On non-2xx the response body is
# dumped to stderr (truncated) so the operator can see WHICH component is
# down (redis / azure_credential / azure_storage / terminal_sidecar)
# without opening another terminal.
# ---------------------------------------------------------------------------
poll_health() {
  local url="https://$CONTAINER_APP_FQDN/api/health/ready"
  local deadline=$(( SECONDS + HEALTH_TIMEOUT ))
  local attempt=0 status=000
  local body_file
  body_file="$(mktemp -t elb-health-body.XXXXXX)"
  trap "rm -f '$body_file'" RETURN
  ts "==> Polling $url (timeout ${HEALTH_TIMEOUT}s)"
  while (( SECONDS < deadline )); do
    attempt=$(( attempt + 1 ))
    status="$(curl -s -o "$body_file" -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || echo 000)"
    if [[ "$status" == "200" ]]; then
      ts "    ✓ /api/health/ready → 200 (attempt $attempt)"
      return 0
    fi
    (( attempt % 4 == 0 )) && ts "    attempt $attempt: $status (sleeping 5s)"
    sleep 5
  done
  warn "Health check timed out — last status: $status"
  if [[ -s "$body_file" ]]; then
    printf '\033[33m--- last /api/health/ready body (truncated to 5KB) ---\033[0m\n' >&2
    head -c 5120 "$body_file" >&2
    printf '\n\033[33m--- end body ---\033[0m\n' >&2
  fi
  return 1
}

# ---------------------------------------------------------------------------
# Helper: restore image refs from a snapshot file via per-sidecar PATCH.
# ---------------------------------------------------------------------------
restore_from_snapshot() {
  local snap="$1"
  [[ -s "$snap" ]] || die "no snapshot at $snap"
  command -v jq >/dev/null 2>&1 || die "jq is required to parse the snapshot"
  local prev_rev
  prev_rev="$(jq -r .revision "$snap")"
  ts "==> Rolling back to images recorded for revision $prev_rev"
  local count=0
  while IFS=$'\t' read -r name image; do
    [[ -n "$name" ]] || continue
    ts "    container=$name → image=$image"
    if $DRY_RUN; then continue; fi
    az containerapp update \
      --name "$CONTAINER_APP_NAME" --resource-group "$AZURE_RESOURCE_GROUP" \
      --container-name "$name" --image "$image" -o none \
      || die "PATCH failed for container=$name (RBAC?)"
    count=$(( count + 1 ))
  done < <(jq -r '.images[] | "\(.name)\t\(.image)"' "$snap")
  ts "    PATCHed $count containers"
}

# ---------------------------------------------------------------------------
# Rollback mode — fast exit before the build pipeline.
# ---------------------------------------------------------------------------
if [[ "$SCOPE" == "rollback" ]]; then
  [[ -s "$SNAPSHOT" ]] || die "no snapshot found at $SNAPSHOT (was an upgrade ever run from this workstation?)"
  if ! $ASSUME_YES; then
    cat <<EOF
Snapshot: $SNAPSHOT
Target app: $CONTAINER_APP_NAME ($CONTAINER_APP_FQDN)
$(jq . "$SNAPSHOT" 2>/dev/null || cat "$SNAPSHOT")

Press ENTER to roll back to the snapshot above, or Ctrl-C to abort.
EOF
    read -r _
  fi
  restore_from_snapshot "$SNAPSHOT"
  if poll_health; then
    ts "✓ Rollback complete. App is healthy."
    exit 0
  fi
  die "Rollback PATCH applied but /api/health/ready did not return 200 within ${HEALTH_TIMEOUT}s."
fi

# ---------------------------------------------------------------------------
# Preflight: working tree + optional git pull.
# ---------------------------------------------------------------------------
if ! git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  die "not inside a git working tree: $REPO_ROOT"
fi
DIRTY="$(git -C "$REPO_ROOT" status --porcelain | head -1 || true)"
CURRENT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
if [[ -n "$DIRTY" ]] && ! $ALLOW_DIRTY; then
  warn "working tree is not clean:"
  git -C "$REPO_ROOT" status --short
  die "refuse to build with uncommitted changes (use --allow-dirty to override)"
fi

if $DO_PULL; then
  TARGET_BRANCH="${TARGET_BRANCH:-$CURRENT_BRANCH}"
  if [[ "$CURRENT_BRANCH" != "$TARGET_BRANCH" ]]; then
    die "--pull would update $CURRENT_BRANCH but --branch is $TARGET_BRANCH; checkout the target branch first."
  fi
  ts "==> git pull --ff-only on $CURRENT_BRANCH"
  if $DRY_RUN; then
    ts "    (dry-run; skipping)"
  else
    git -C "$REPO_ROOT" pull --ff-only || die "git pull --ff-only failed (non-fast-forward?). Rebase manually and retry."
  fi
fi

HEAD_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)"
[[ -n "$TAG" ]] || TAG="$(date +%Y%m%d%H%M%S)-${HEAD_SHA}"

# ---------------------------------------------------------------------------
# Plan summary + confirm.
# ---------------------------------------------------------------------------
cat <<EOF
============================================================
CLI rolling-update plan
  App:         $CONTAINER_APP_NAME ($CONTAINER_APP_FQDN)
  RG:          $AZURE_RESOURCE_GROUP
  Scope:       $SCOPE
  Branch:      $CURRENT_BRANCH @ $HEAD_SHA $( $DO_PULL && echo "(after git pull --ff-only)")
  Tag:         $TAG
  Snapshot:    $SNAPSHOT
  Health:      https://$CONTAINER_APP_FQDN/api/health/ready  (timeout ${HEALTH_TIMEOUT}s)
  Auto-rollback on failure: $( $AUTO_ROLLBACK && echo yes || echo "no — manual recovery only")
  Dry-run:     $( $DRY_RUN && echo yes || echo no)
============================================================
EOF

if ! $ASSUME_YES && ! $DRY_RUN; then
  printf 'Proceed? [y/N] '
  read -r ANSWER
  [[ "$ANSWER" == "y" || "$ANSWER" == "Y" ]] || die "aborted by user"
fi

take_snapshot "$SNAPSHOT"

# ---------------------------------------------------------------------------
# Dispatch.
# ---------------------------------------------------------------------------
deploy_one() {
  local sidecar="$1"
  ts "==> Deploying sidecar=$sidecar tag=$TAG"
  if $DRY_RUN; then
    ts "    (dry-run; would run: scripts/dev/quick-deploy.sh $sidecar $TAG)"
    return 0
  fi
  bash "$REPO_ROOT/scripts/dev/quick-deploy.sh" "$sidecar" "$TAG"
}

deploy_full() {
  ts "==> Running postprovision.sh (full 3-image build + template swap)"
  if $DRY_RUN; then
    ts "    (dry-run; would run: scripts/dev/postprovision.sh)"
    return 0
  fi
  bash "$REPO_ROOT/scripts/dev/postprovision.sh"
}

case "$SCOPE" in
  api|frontend|terminal) deploy_one "$SCOPE" ;;
  full)                  deploy_full ;;
  *)                     die "internal: unhandled scope $SCOPE" ;;
esac

if $DRY_RUN; then
  ts "==> Dry-run complete. No build was run, no PATCH applied."
  exit 0
fi

# ---------------------------------------------------------------------------
# Verify + (optional) auto-rollback.
# ---------------------------------------------------------------------------
if poll_health; then
  ts "✓ Upgrade complete and healthy."
  ts "  Roll back later with: $0 rollback --yes"
  if $TAIL_LOGS; then
    ts "==> Tailing api logs (Ctrl-C to exit)"
    az containerapp logs show --name "$CONTAINER_APP_NAME" --resource-group "$AZURE_RESOURCE_GROUP" \
      --container api --follow --tail 20 || true
  fi
  exit 0
fi

warn "==> /api/health/ready did not return 200 within ${HEALTH_TIMEOUT}s."
if $AUTO_ROLLBACK; then
  warn "==> Auto-rollback enabled — restoring previous image refs from $SNAPSHOT"
  restore_from_snapshot "$SNAPSHOT"
  if poll_health; then
    ts "✓ Rollback complete. App is healthy again on the previous tag."
    die "Original upgrade failed health check. Investigate the new tag in ACR before retrying."
  fi
  die "Rollback PATCH applied but /api/health/ready still fails. Investigate immediately."
fi

cat <<EOF >&2

Manual rollback (auto-rollback was disabled):

  $0 rollback --yes

Or run the per-sidecar PATCHes shown in the snapshot file:
  $SNAPSHOT
EOF
exit 1
