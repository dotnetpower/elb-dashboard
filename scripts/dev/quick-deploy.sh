#!/usr/bin/env bash
# Quick single-sidecar deploy for the bundled Container App.
#
# When a code-only fix in api/ or web/ or terminal/ needs to land on the
# real Azure revision, running the full postprovision (3 parallel ACR
# builds + a Bicep redeploy of all six sidecars) takes 5-10 minutes. This
# script does a far smaller cycle:
#
#   1. Build ONE image via `az acr build` (cached layers, ~30-90 s).
#   2. Patch ONLY that container's image via `az containerapp update`
#      (one ARM transaction, ~20-30 s — does NOT touch sidecar layout,
#      secrets, probes, or scale rules).
#   3. (Optional) tail the new revision's logs.
#
# It refuses to touch sidecar structure (secrets, probes, volumes) — for
# those changes you still need a Bicep redeploy via postprovision.sh
# or `az deployment group create --template-file containerAppControl.bicep`.
# The frontend sidecar is the only exception for env vars: its runtime
# config is generated from server environment variables at startup, so
# this script keeps those values in sync during fast frontend deploys.
#
# Control-plane GUARD env exception: api/worker/beat PATCHes also upsert the
# policy toggles from infra/control-plane-env.json (ENFORCE_DASHBOARD_RBAC,
# ENFORCE_OPENAPI_EXEC_RBAC, BLAST_GATE_ENABLED, BLAST_JOBS_SHARED_VISIBILITY,
# STRICT_BLUEGREEN, OPENAPI_ALLOW_PUBLIC_LB). That same JSON is the source
# Bicep loads, so a guard-default change lands on BOTH a full `azd provision`
# AND a fast / GitHub-Actions deploy. All other runtime env stays untouched.
#
# Usage:
#   scripts/dev/quick-deploy.sh <sidecar> [tag]
#
# Sidecars: api | worker | beat | frontend | terminal | all
#   (worker and beat reuse the api image — passing either rebuilds api
#    and points the worker / beat container at the new tag.)
#   (all deploys api, frontend, and terminal in sequence; api also patches
#    worker and beat.)
#
# Examples:
#   scripts/dev/quick-deploy.sh api
#   scripts/dev/quick-deploy.sh all
#   scripts/dev/quick-deploy.sh terminal
#   scripts/dev/quick-deploy.sh frontend custom-tag-123
#   scripts/dev/quick-deploy.sh api --logs        # tail after deploy
#   scripts/dev/quick-deploy.sh all --logs        # tail api logs after all deploys
#   scripts/dev/quick-deploy.sh terminal --rebuild-terminal-base
#
# Required env (export them or `source /tmp/azd-env.sh`):
#   AZURE_RESOURCE_GROUP         e.g. rg-elb-dashboard
#   ACR_NAME                     short name (no .azurecr.io)
#   ACR_LOGIN_SERVER             e.g. crelbXYZ.azurecr.io
#   CONTAINER_APP_NAME           e.g. ca-elb-dashboard

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
. "$REPO_ROOT/scripts/dev/acr-build-access.sh"
. "$REPO_ROOT/scripts/dev/terminal-base-image.sh"
. "$REPO_ROOT/scripts/dev/az-context.sh"

ts() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Control-plane GUARD/POLICY env toggles (single source of truth shared with
# infra/modules/containerAppControl.bicep via infra/control-plane-env.json).
#
# Why: this script patches IMAGES only. Container App env vars otherwise land
# exclusively through a full `azd provision` / postprovision Bicep deploy. So
# a guard default changed in Bicep (e.g. ENFORCE_DASHBOARD_RBAC=true) would
# never reach a fast deploy OR the GitHub Actions deploy.yml path (which also
# calls this script) — a no-RBAC user could still load the dashboard after an
# apparent redeploy. We read the SAME JSON Bicep reads and apply the per-
# sidecar guard toggles as `--set-env-vars` on every api/worker/beat PATCH so
# both deploy paths converge to the repo's source of truth. `--set-env-vars`
# is an upsert: it only touches the listed keys, leaving image/secret/other
# env entries intact.
# ---------------------------------------------------------------------------
CONTROL_PLANE_ENV_FILE="$REPO_ROOT/infra/control-plane-env.json"

# Fail fast on a malformed source file (runs in the parent shell so `die`
# actually aborts). A missing file is tolerated (older checkouts) — the
# per-sidecar helper then yields no pairs and the PATCH stays image-only.
if [[ -f "$CONTROL_PLANE_ENV_FILE" ]]; then
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$CONTROL_PLANE_ENV_FILE" \
    || die "control-plane-env: $CONTROL_PLANE_ENV_FILE is not valid JSON"
fi

# Echo `KEY=VALUE` lines for the given sidecar (api|worker|beat|...), or
# nothing when the file is absent or the sidecar has no guard toggles.
#
# Per-deployment override: when a control-plane key is ALSO present in the
# process environment (e.g. exported from azd env), that value wins over the
# repo default in control-plane-env.json. This keeps the repo default OFF for
# opt-in guards (charter §12a Rule 4) while letting a specific deployment pin a
# toggle (e.g. SERVICEBUS_ENABLED=true) so it survives every redeploy instead of
# being reset to the JSON default. Set-vs-unset is tested explicitly (a key
# absent from the environment falls through to the JSON value; an exported empty
# string is honoured as an intentional override) to avoid the `${!key:-}`
# empty-vs-unset bug class.
control_plane_env_pairs() {
  local sidecar="$1"
  [[ -f "$CONTROL_PLANE_ENV_FILE" ]] || return 0
  python3 - "$CONTROL_PLANE_ENV_FILE" "$sidecar" <<'PY'
import json, os, sys
path, sidecar = sys.argv[1], sys.argv[2]
data = json.load(open(path))
section = data.get(sidecar) or {}
for k, v in section.items():
    if k.startswith("_"):
        continue
    # azd-env / process-env override wins when the key is SET (even to ""),
    # otherwise fall back to the repo default from the JSON.
    value = os.environ[k] if k in os.environ else v
    print(f"{k}={value}")
PY
}


release_build_number() {
  local latest_tag=""
  latest_tag="$(git -C "$REPO_ROOT" tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname --merged HEAD 2>/dev/null | head -n1 || true)"
  if [[ -n "$latest_tag" ]]; then
    git -C "$REPO_ROOT" rev-list --count "$latest_tag..HEAD" 2>/dev/null || printf '0\n'
  else
    git -C "$REPO_ROOT" rev-list --count HEAD 2>/dev/null || printf '0\n'
  fi
}

# Env-loading helpers (strip_quotes / load_simple_env_file / load_azd_env)
# live in lib-env.sh so the set-vs-unset guard cannot drift back to the
# buggy `${!key:-}` form — see lib-env.sh "Risky contracts".
. "$REPO_ROOT/scripts/dev/lib-env.sh"

provider_registration_marker() {
  printf '%s/.logs/provider-registration.%s.ok' "$REPO_ROOT" "${AZURE_SUBSCRIPTION_ID:-default}"
}

ensure_provider_registration_once() {
  local marker max_age now mtime age
  if [[ "${SKIP_PROVIDER_REGISTRATION:-false}" == "true" ]]; then
    ts "Skipping provider registration (SKIP_PROVIDER_REGISTRATION=true)"
    return 0
  fi
  marker="$(provider_registration_marker)"
  max_age="${PROVIDER_REGISTRATION_MARKER_TTL_SECONDS:-3600}"
  if [[ -f "$marker" && "$max_age" =~ ^[0-9]+$ ]]; then
    now="$(date +%s)"
    mtime="$(stat -c %Y "$marker" 2>/dev/null || printf '0')"
    age=$(( now - mtime ))
    if [[ "$age" -ge 0 && "$age" -lt "$max_age" ]]; then
      ts "Skipping provider registration (cached ${age}s ago)"
      return 0
    fi
  fi
  mkdir -p "$(dirname "$marker")"
  if [[ -n "${AZURE_SUBSCRIPTION_ID:-}" ]]; then
    bash "$REPO_ROOT/scripts/dev/register-providers.sh" --subscription "$AZURE_SUBSCRIPTION_ID"
  else
    bash "$REPO_ROOT/scripts/dev/register-providers.sh"
  fi
  : > "$marker"
}

# The auth-bypass toggles (VITE_AUTH_DEV_BYPASS / AUTH_DEV_BYPASS) are
# local-debug-only — set by scripts/dev/local-debug-auth.sh and frequently
# left behind in .env / .env.local after a local session. They must NEVER be
# imported from a file into a cloud deploy (doing so bakes an MSAL-skipping
# SPA / bearer-skipping api into the Container App — see the guard below and
# docs/features_change/2026-05/2026-05-25-frontend-env-leak-hardening.md).
# A developer who genuinely wants the bypass in cloud exports it explicitly
# on the command line (an existing shell export wins over the file import) and
# also sets ELB_ALLOW_AUTH_BYPASS_IN_CLOUD=1.
_ELB_AUTH_BYPASS_SKIP=(VITE_AUTH_DEV_BYPASS AUTH_DEV_BYPASS)
load_simple_env_file "$REPO_ROOT/.env" "${_ELB_AUTH_BYPASS_SKIP[@]}"
load_simple_env_file "$REPO_ROOT/.env.local" "${_ELB_AUTH_BYPASS_SKIP[@]}"
load_simple_env_file "$REPO_ROOT/web/.env.production" "${_ELB_AUTH_BYPASS_SKIP[@]}"
# web/.env.local exists for local-dev (vite dev server + local-run.sh web)
# and pins VITE_API_BASE_URL=http://localhost:8085 plus local-debug toggles
# (VITE_AUTH_DEV_BYPASS, AUTH_DEV_BYPASS). It may also carry a developer's
# personal MSAL tenant/client for local SPA debugging. Those values must
# NEVER end up in a cloud frontend's runtime-config.js or container env —
# see the guard below and
# docs/features_change/2026-05/2026-05-25-frontend-env-leak-hardening.md.
load_simple_env_file "$REPO_ROOT/web/.env.local" \
  VITE_API_BASE_URL \
  VITE_AUTH_DEV_BYPASS \
  AUTH_DEV_BYPASS \
  VITE_AZURE_TENANT_ID \
  VITE_AZURE_CLIENT_ID \
  VITE_AZURE_REDIRECT_URI \
  API_CLIENT_ID
if [[ -z "${AZURE_RESOURCE_GROUP:-}" || -z "${ACR_NAME:-}" || -z "${ACR_LOGIN_SERVER:-}" || -z "${CONTAINER_APP_NAME:-}" ]]; then
  load_azd_env
fi

[[ $# -ge 1 ]] || die "usage: $0 <api|worker|beat|frontend|terminal|all> [tag] [--logs] [--rebuild-terminal-base] [--no-build|--build-only] [--yes]"

SIDECAR="$1"; shift || true
TAG=""
TAIL_LOGS=false
REBUILD_TERMINAL_BASE=false
SKIP_CONFIRM=false
# --no-build: skip the `az acr build` step and patch the Container App
# straight to an EXISTING image tag in ACR. Used by the GitHub Actions
# deploy workflow, which builds in a separate `build-images.yml` job and
# then triggers deploy.yml with the resulting tag. When set, the frontend
# PATCH also skips --set-env-vars so the runtime env baked at build time
# (or applied by the last full deploy) is preserved.
NO_BUILD=false
# --build-only: opposite of --no-build. Build the image(s) via `az acr build`
# and skip the `az containerapp update` PATCH. Used by build-images.yml in
# GitHub Actions so a push to main produces images in ACR without changing
# the running Container App; deploy.yml then triggers a separate run with
# --no-build to actually swap the revision.
BUILD_ONLY=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --logs) TAIL_LOGS=true ;;
    --rebuild-terminal-base) REBUILD_TERMINAL_BASE=true ;;
    --no-build) NO_BUILD=true ;;
    --build-only) BUILD_ONLY=true ;;
    --yes|-y) SKIP_CONFIRM=true ;;
    -*)     die "unknown flag: $1" ;;
    *)      TAG="$1" ;;
  esac
  shift
done
$NO_BUILD && $BUILD_ONLY && die "--no-build and --build-only are mutually exclusive"
[[ -n "$TAG" ]] || TAG="$(date +%Y%m%d%H%M%S)"
# ELB_QUICK_DEPLOY_SKIP_CONFIRM=1 (env) is an alternative to --yes for
# automation contexts that cannot easily inject a CLI flag (e.g. CI hooks
# that re-shell into this script).
[[ "${ELB_QUICK_DEPLOY_SKIP_CONFIRM:-0}" == "1" ]] && SKIP_CONFIRM=true

# ---------------------------------------------------------------------------
# Interactive confirmation. Show the discovered subscription/tenant/RG/ACR/
# app so the operator can sanity-check the target before any ACR build or
# Container App PATCH runs. Skipped when:
#   - stdin is not a TTY (CI, piped, etc.)
#   - --yes / -y is passed on the CLI
#   - ELB_QUICK_DEPLOY_SKIP_CONFIRM=1 is exported
#
# Default-Enter = proceed, anything else = abort. The default is "proceed"
# because the alternative (default-abort) would force every operator to
# type a key on every deploy, even when the discovered target is exactly
# what `az account show` already told them on the previous line. No input
# within 10 s also proceeds, so an unattended run is never left blocking on
# the prompt.
# ---------------------------------------------------------------------------
confirm_deploy_target() {
  $SKIP_CONFIRM && return 0
  [[ -t 0 ]] || return 0
  printf '\n' >&2
  printf '\033[1m==> About to deploy to:\033[0m\n' >&2
  printf '      subscription : %s  (%s)\n' "${AZURE_SUBSCRIPTION_ID:-?}" "$(az account show --query name -o tsv 2>/dev/null || printf '?')" >&2
  printf '      tenant       : %s\n' "${AZURE_TENANT_ID:-?}" >&2
  printf '      resourceGroup: %s\n' "${AZURE_RESOURCE_GROUP:-?}" >&2
  printf '      acr          : %s\n' "${ACR_LOGIN_SERVER:-${ACR_NAME:-?}}" >&2
  printf '      containerApp : %s\n' "${CONTAINER_APP_NAME:-?}" >&2
  [[ -n "${CONTAINER_APP_FQDN:-}" ]] && printf '      fqdn         : https://%s\n' "$CONTAINER_APP_FQDN" >&2
  printf '      sidecar(s)   : %s\n' "$SIDECAR" >&2
  printf '      tag          : %s\n\n' "$TAG" >&2
  local reply=""
  # 10 s auto-proceed: no input within the window is treated the same as
  # pressing Enter (proceed). `read -t` returns non-zero on timeout while
  # leaving $reply empty, so the existing "empty == proceed" branch covers it.
  if read -r -t 10 -p "Proceed? [Enter=yes, anything else=abort, auto-yes in 10s] " reply; then
    if [[ -n "$reply" ]]; then
      ts "aborted by user (input: '$reply')"
      exit 1
    fi
  else
    printf '\n' >&2
    ts "no input within 10s — proceeding automatically"
  fi
}

# ---------------------------------------------------------------------------
# preflight_permission_check (critique #8) — fail fast with a clear
# remediation message when the caller lacks the four ARM read permissions
# the script will need a few seconds later: read on the resource group,
# read on the ACR, read on the Container App, and an `az acr build`
# preflight (which exercises both ACR read and AcrPush). The read probes
# are cheap (~200 ms each) so the cost is negligible; the value is that
# a 401 / 403 surfaces here with the exact role the operator needs
# instead of after a 30-90 s build.
#
# Skip entirely with ELB_QUICK_DEPLOY_SKIP_PREFLIGHT=1 (CI runners with
# pre-validated SPs do not need this).
# ---------------------------------------------------------------------------
preflight_permission_check() {
  [[ "${ELB_QUICK_DEPLOY_SKIP_PREFLIGHT:-0}" == "1" ]] && return 0
  command -v az >/dev/null 2>&1 || die "az CLI not found on PATH"

  local who="" user_type=""
  who="$(az account show --query 'user.name' -o tsv 2>/dev/null || true)"
  user_type="$(az account show --query 'user.type' -o tsv 2>/dev/null || true)"
  if [[ -z "$who" ]]; then
    die "Not signed in to Azure CLI. Run 'az login' and retry."
  fi
  # Critique-round-1 M2: differentiate user-vs-SP in the diagnostic so
  # a service-principal session in CI does not see a misleading
  # "Run az login" hint when its assignments are missing.
  if [[ "$user_type" == "servicePrincipal" ]]; then
    ts "preflight: signed-in as service principal $who"
  else
    ts "preflight: signed-in as $who"
  fi

  local _hint_who="$who"
  if [[ "$user_type" == "servicePrincipal" ]]; then
    _hint_who="<sp-object-id>"  # az role assignment list --assignee expects the SP object id, not appId
  fi

  if ! az group show -n "$AZURE_RESOURCE_GROUP" -o none 2>/dev/null; then
    die "Cannot read resource group '$AZURE_RESOURCE_GROUP'. The signed-in identity needs at least 'Reader' on the subscription or RG. Run 'az role assignment list --assignee $_hint_who --resource-group $AZURE_RESOURCE_GROUP' to inspect."
  fi

  if ! az acr show -n "$ACR_NAME" -g "$AZURE_RESOURCE_GROUP" -o none 2>/dev/null; then
    die "Cannot read ACR '$ACR_NAME' in '$AZURE_RESOURCE_GROUP'. The signed-in identity needs 'Reader' (or higher). Without 'Contributor' the subsequent 'az acr update' (firewall toggle) and 'az acr build' will fail with AuthorizationFailed."
  fi

  if ! az containerapp show -n "$CONTAINER_APP_NAME" -g "$AZURE_RESOURCE_GROUP" -o none 2>/dev/null; then
    die "Cannot read Container App '$CONTAINER_APP_NAME' in '$AZURE_RESOURCE_GROUP'. The signed-in identity needs 'Contributor' on the Container App for the upcoming 'az containerapp update' to succeed."
  fi

  ts "preflight: ARM read access OK on rg/acr/containerApp"
}

# ---------------------------------------------------------------------------
# ensure_workspace_tags -- add the elb-* workspace discovery tags to the
# deployment resource group when they are missing.
#
# The SPA's first-run auto-discovery (web/src/pages/Dashboard/configFromTags.ts)
# only treats a resource group as a BLAST workspace when it carries at least
# one `elb-*` tag, and reads `elb-storage` / `elb-acr` / `elb-region` from
# those tags to populate the dashboard. The full `azd up` path applies these
# via postprovision.sh `tag_workspace_resource_group`, but a fast
# `quick-deploy.sh` cycle never ran provisioning, so a resource group that
# was only ever touched by quick-deploy (or had its tags stripped) leaves
# every signed-in user stuck on the Setup Wizard even when they hold read
# access. This closes that gap.
#
# "Add if missing" semantics: each desired key is written ONLY when it is
# absent (or empty) on the RG, so a pre-existing correct value is never
# clobbered by a stale shell variable. Keys whose value cannot be resolved
# from the environment are skipped rather than written empty. The merge is
# best-effort — a caller without tag-write permission (Reader) gets a warn
# line, not a failed deploy. Skip entirely with ELB_SKIP_WORKSPACE_TAGS=1.
# ---------------------------------------------------------------------------
ensure_workspace_tags() {
  if [[ "${ELB_SKIP_WORKSPACE_TAGS:-0}" == "1" ]]; then
    ts "Skipping workspace RG tagging (ELB_SKIP_WORKSPACE_TAGS=1)"
    return 0
  fi

  local rg_id
  rg_id="$(az group show -n "$AZURE_RESOURCE_GROUP" --query id -o tsv --only-show-errors 2>/dev/null || true)"
  if [[ -z "$rg_id" ]]; then
    ts "    ! cannot resolve resource group id for tagging; skipping workspace tags"
    return 0
  fi

  # Desired discovery tags, mirroring postprovision.sh tag_workspace_resource_group.
  # An empty value means "could not resolve" — we never write an empty tag.
  local -a keys=(elb-workload-rg elb-acr-rg elb-acr elb-storage elb-region)
  local -A desired=(
    [elb-workload-rg]="$AZURE_RESOURCE_GROUP"
    [elb-acr-rg]="$AZURE_RESOURCE_GROUP"
    [elb-acr]="${ACR_NAME:-}"
    [elb-storage]="${STORAGE_ACCOUNT_NAME:-}"
    [elb-region]="${AZURE_LOCATION:-}"
  )

  local -a merge_args=()
  local k v present
  for k in "${keys[@]}"; do
    v="${desired[$k]}"
    [[ -n "$v" ]] || continue
    # Query the single tag value; az prints empty (not "None") for an
    # absent key with `-o tsv`.
    present="$(az group show -n "$AZURE_RESOURCE_GROUP" \
      --query "tags.\"$k\"" -o tsv --only-show-errors 2>/dev/null || true)"
    if [[ -z "$present" || "$present" == "None" ]]; then
      merge_args+=("$k=$v")
    fi
  done

  if [[ ${#merge_args[@]} -eq 0 ]]; then
    ts "==> Workspace RG discovery tags already present; nothing to add"
    return 0
  fi

  ts "==> Adding missing dashboard workspace tags: ${merge_args[*]}"
  if az tag update \
      --resource-id "$rg_id" \
      --operation Merge \
      --tags "${merge_args[@]}" \
      --only-show-errors >/dev/null 2>&1; then
    ts "    ✓ workspace discovery tags merged onto $AZURE_RESOURCE_GROUP"
  else
    ts "    ! tag merge failed (need 'Tag Contributor' or 'Contributor' on the RG); auto-discovery may keep showing the Setup Wizard"
  fi
}

# ---------------------------------------------------------------------------
# resolve_image_digest -- pin a mutable tag to its immutable digest.
#
# Azure Container Apps only rolls a NEW revision when the template's image
# string changes. Patching a mutable tag (latest-main, latest, ...) that the
# active revision already references is a byte-for-byte no-op, so a freshly
# rebuilt image pushed under the SAME tag is silently ignored -- the deploy
# "succeeds" but the old image keeps running and the version stamp never
# changes. Resolving the tag to registry/image@sha256:... makes every
# distinct build a distinct template -> a new revision always rolls. Falls
# back to the tag ref (with a warning) when the manifest lookup fails, so a
# transient ACR read error degrades to the old behaviour rather than
# aborting the deploy.
# ---------------------------------------------------------------------------
resolve_image_digest() {
  local ref="$1" digest
  digest="$(az acr manifest show-metadata "$ref" --query digest -o tsv 2>/dev/null | tr -d '[:space:]')" || true
  if [[ "$digest" == sha256:* ]]; then
    printf '%s@%s' "${ref%:*}" "$digest"
  else
    printf 'WARN: could not resolve digest for %s; patching with the mutable tag (a re-pushed tag may not roll a new revision)\n' "$ref" >&2
    printf '%s' "$ref"
  fi
}

# ---------------------------------------------------------------------------
# assert_msal_client_matches_target -- refuse to bake a frontend whose MSAL
# App Registration client id (VITE_AZURE_CLIENT_ID) does not match the one
# the TARGET Container App's api sidecar already validates bearer tokens
# against (its API_CLIENT_ID env).
#
# Why: .env / web/.env.local in a fresh clone frequently carry a developer's
# OWN tenant/client values (see the env-leak hardening note
# docs/features_change/2026-05/2026-05-25-frontend-env-leak-hardening.md).
# When a different operator runs `quick-deploy.sh all` / `frontend`
# without exporting the target's MSAL overrides, the SPA is baked to log
# users in against App Registration A while the api only accepts tokens
# minted for App Registration B -- the deploy "succeeds" but every /api/*
# call returns 401. The existing localhost / auth-bypass guards above catch
# two siblings of this incident class; this catches the wrong-tenant one.
#
# Behaviour (mirrors the abort-with-escape-hatch style of the auth-bypass
# guard, NOT a default-OFF STRICT_* gate -- baking a mismatched audience is
# always a bug, so the safe default is to stop):
#   * target api API_CLIENT_ID present AND differs from the value to bake
#       -> abort with remediation + escape hatch ELB_ALLOW_MSAL_CLIENT_MISMATCH=1
#         (the intended path when deliberately rotating the App Registration).
#   * target api API_CLIENT_ID absent (first-ever deploy / bootstrap) OR the
#     show query fails (transient ARM error / read-only hiccup) -> warn and
#     continue, so a legitimate first rollout is never blocked.
#
# Args: $1 = client id that will be baked into the frontend (API_CLIENT_ID_VAL).
# ---------------------------------------------------------------------------
assert_msal_client_matches_target() {
  local baking="$1" current=""
  [[ -n "$baking" ]] || return 0
  if [[ "${ELB_ALLOW_MSAL_CLIENT_MISMATCH:-0}" == "1" ]]; then
    ts "MSAL client-id match check skipped (ELB_ALLOW_MSAL_CLIENT_MISMATCH=1)"
    return 0
  fi
  current="$(az containerapp show -n "$CONTAINER_APP_NAME" -g "$AZURE_RESOURCE_GROUP" \
    --query "properties.template.containers[?name=='api'].env[] | [?name=='API_CLIENT_ID'].value | [0]" \
    -o tsv 2>/dev/null | tr -d '[:space:]')" || true
  if [[ -z "$current" || "$current" == "None" ]]; then
    ts "MSAL client-id match check: target api has no API_CLIENT_ID yet (first deploy?) — skipping"
    return 0
  fi
  if [[ "$current" != "$baking" ]]; then
    die "MSAL client-id mismatch: about to bake VITE_AZURE_CLIENT_ID='$baking' into the cloud frontend, but the target Container App's api sidecar validates bearer tokens against API_CLIENT_ID='$current'. Deploying would log users in against the wrong App Registration — every /api/* call returns 401. Fix the source value (.env / web/.env.local / azd env) so VITE_AZURE_CLIENT_ID/API_CLIENT_ID matches the target, or set ELB_ALLOW_MSAL_CLIENT_MISMATCH=1 if you are intentionally rotating the App Registration."
  fi
  ts "MSAL client-id match check OK (frontend VITE_AZURE_CLIENT_ID == target api API_CLIENT_ID)"
}

if [[ "$SIDECAR" == "all" ]]; then
  # ---------------------------------------------------------------------------
  # Parallel-build path: api / frontend / terminal images build concurrently
  # via three backgrounded `az acr build` jobs, then we PATCH the Container
  # App containers SEQUENTIALLY. Two races make naive full parallelism
  # unsafe and they're both worth re-stating:
  #
  #   1. ACR firewall toggle. acr_ensure_build_access / acr_restore_build_access
  #      track state in subshell-local vars. If each per-target subshell ran
  #      its own toggle, the first one to finish would close the firewall
  #      while the others were still mid-build, and they'd fail with 401 /
  #      "network not allowed". Open ONCE in the parent, close ONCE after
  #      `wait`.
  #
  #   2. `az containerapp update --container-name X` is read-modify-write
  #      against the same template with no ETag protection. Running three
  #      PATCHes in parallel against a single Container App is a classic
  #      last-write-wins race -- some sidecars get reverted on the final
  #      active revision. PATCHes stay sequential.
  #
  # Net result vs the old recursive-sequential loop: build time drops from
  # ~3 min (3 x ~60 s sequential) to ~60-90 s (parallel; bound by the
  # slowest image). PATCH wall time is unchanged (~1 min). Total deploy
  # ~3-4 min vs ~6-8 min.
  # ---------------------------------------------------------------------------
  ts "==> Deploying all quick-deploy targets with tag: $TAG (parallel-build mode)"
  $NO_BUILD && ts "    --no-build: skipping ACR build, will only PATCH Container App"

  # Discover/align env from the active az login BEFORE validating env vars,
  # so a stale `/tmp/azd-env.sh` from a different sub does not block the
  # deploy. The helper exports AZURE_*, ACR_*, CONTAINER_APP_*, etc. from
  # ARM lookups in the active subscription.
  assert_az_subscription_aligned

  for v in AZURE_RESOURCE_GROUP ACR_NAME ACR_LOGIN_SERVER CONTAINER_APP_NAME; do
    [[ -n "${!v:-}" ]] || die "$v is unset and az-context discovery could not populate it (run: az login + verify the active sub has an elb-dashboard RG)"
  done
  confirm_deploy_target
  preflight_permission_check
  ensure_provider_registration_once
  ensure_workspace_tags

  NEW_API="${ACR_LOGIN_SERVER}/elb-api:${TAG}"
  NEW_FRONTEND="${ACR_LOGIN_SERVER}/elb-frontend:${TAG}"
  NEW_TERMINAL="${ACR_LOGIN_SERVER}/elb-terminal:${TAG}"

if ! $NO_BUILD; then
  # Resolve frontend build args + per-PATCH env-vars on the host once. These
  # mirror the single-sidecar `frontend` branch below (line ~228 onward) and
  # MUST stay in sync with it -- if you add a new VITE_FEATURE_* there, add
  # it here too.
  API_CLIENT_ID_VAL="${VITE_AZURE_CLIENT_ID:-${API_CLIENT_ID:-}}"
  [[ -n "$API_CLIENT_ID_VAL" ]] || die "API_CLIENT_ID/VITE_AZURE_CLIENT_ID is unset; set .env, web/.env.local, or azd env before deploying"
  AZURE_TENANT_ID_VAL="${VITE_AZURE_TENANT_ID:-${AZURE_TENANT_ID:-common}}"
  if [[ "$AZURE_TENANT_ID_VAL" == "common" && -n "${AZURE_TENANT_ID:-}" ]]; then
    AZURE_TENANT_ID_VAL="$AZURE_TENANT_ID"
  fi
  VITE_AUTH_DEV_BYPASS_VAL="${VITE_AUTH_DEV_BYPASS:-false}"
  VITE_API_BASE_URL_VAL="${VITE_API_BASE_URL:-}"
  VITE_AZURE_REDIRECT_URI_VAL="${VITE_AZURE_REDIRECT_URI:-__RUNTIME__}"
  VITE_FEATURE_CUSTOM_DB_VAL="${VITE_FEATURE_CUSTOM_DB:-true}"
  VITE_FEATURE_LAB_TOOLS_VAL="${VITE_FEATURE_LAB_TOOLS:-true}"
  VITE_FEATURE_TERMINAL_VAL="${VITE_FEATURE_TERMINAL:-true}"

  if [[ -n "$VITE_API_BASE_URL_VAL" ]] && \
     [[ "$VITE_API_BASE_URL_VAL" =~ ^https?://(localhost|127\.|0\.0\.0\.0|\[::1\]) ]]; then
    die "VITE_API_BASE_URL='$VITE_API_BASE_URL_VAL' points at the local host — refusing to bake that into the cloud frontend. Run 'unset VITE_API_BASE_URL' (or export VITE_API_BASE_URL='') and retry."
  fi
  if [[ "$VITE_AUTH_DEV_BYPASS_VAL" == "true" && "${ELB_ALLOW_AUTH_BYPASS_IN_CLOUD:-0}" != "1" ]]; then
    die "VITE_AUTH_DEV_BYPASS=true — refusing to deploy a cloud frontend that skips MSAL while the api enforces bearer tokens. Run 'unset VITE_AUTH_DEV_BYPASS' (or export VITE_AUTH_DEV_BYPASS=false) and retry."
  fi
  # Guard: a stale .env / web/.env.local carrying a different tenant's MSAL
  # client id would bake an SPA that authenticates against the wrong App
  # Registration -> 401 on every /api/* call. Stop unless the target api has
  # no client id yet or the operator is deliberately rotating it.
  assert_msal_client_matches_target "$API_CLIENT_ID_VAL"

  APP_VERSION_VAL="${APP_VERSION:-$(node -p "require('$REPO_ROOT/web/package.json').version" 2>/dev/null || echo 0.0.0)}"
  APP_BUILD_NUMBER_VAL="${APP_BUILD_NUMBER:-$(release_build_number)}"
  GIT_COMMIT_VAL="${GIT_COMMIT:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)}"
  BUILD_TIME_VAL="${BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

  LOG_DIR="$REPO_ROOT/.logs/quick-deploy/$TAG"
  mkdir -p "$LOG_DIR"
  ts "==> Per-image build logs:   $LOG_DIR/build-<image>.log"
  ts "    Follow live in another terminal:"
  ts "      tail -F $LOG_DIR/build-*.log"
  ts ""

  # Install the restore trap BEFORE opening the firewall. acr_ensure_build_access
  # mutates ACR network state then waits for the policy to take effect; if any
  # step inside the helper fails (set -e), we must still restore. The helper
  # uses ACR_BUILD_ACCESS_RESTORE_NEEDED so a pre-open trap is a safe no-op.
  trap 'acr_restore_build_access "$ACR_NAME"' EXIT
  acr_ensure_build_access "$ACR_NAME"

  # Resolve terminal base in the parent so the three build subshells don't
  # race on `ensure_terminal_base_image` (it can build + push a base image
  # on cache miss, and two concurrent runs of that helper would step on
  # each other's `az acr import` / `az acr build`). When the base image is
  # missing this is the longest single step of the deploy (~2-4 min); the
  # tip above already pointed the operator at $LOG_DIR/build-elb-terminal-base.log.
  TERMINAL_BASE_REBUILD="$REBUILD_TERMINAL_BASE" ensure_terminal_base_image
  TERMINAL_BASE_IMAGE_VAL="$(terminal_base_image)"

  ts "==> Building 3 images in parallel via az acr build"
  {
    echo "[build-elb-api] starting at $(date -u +%H:%M:%S)"
    az acr build \
      --registry "$ACR_NAME" \
      --image "elb-api:${TAG}" \
      --file "api/Dockerfile" \
      --build-arg "APP_VERSION=$APP_VERSION_VAL" \
      --build-arg "APP_GIT_COMMIT=$GIT_COMMIT_VAL" \
      --build-arg "APP_BUILD_TIME=$BUILD_TIME_VAL" \
      "." \
      -o none
    rc=$?
    echo "[build-elb-api] finished at $(date -u +%H:%M:%S), rc=$rc"
    exit $rc
  } > "$LOG_DIR/build-elb-api.log" 2>&1 &
  PID_API=$!

  {
    echo "[build-elb-frontend] starting at $(date -u +%H:%M:%S)"
    az acr build \
      --registry "$ACR_NAME" \
      --image "elb-frontend:${TAG}" \
      --file "web/Dockerfile" \
      --build-arg "VITE_API_BASE_URL=$VITE_API_BASE_URL_VAL" \
      --build-arg "VITE_AUTH_DEV_BYPASS=$VITE_AUTH_DEV_BYPASS_VAL" \
      --build-arg "VITE_AZURE_REDIRECT_URI=$VITE_AZURE_REDIRECT_URI_VAL" \
      --build-arg "VITE_AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL" \
      --build-arg "VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL" \
      --build-arg "VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB_VAL" \
      --build-arg "VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS_VAL" \
      --build-arg "VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL_VAL" \
      --build-arg "APP_VERSION=$APP_VERSION_VAL" \
      --build-arg "APP_BUILD_NUMBER=$APP_BUILD_NUMBER_VAL" \
      --build-arg "GIT_COMMIT=$GIT_COMMIT_VAL" \
      --build-arg "BUILD_TIME=$BUILD_TIME_VAL" \
      "." \
      -o none
    rc=$?
    echo "[build-elb-frontend] finished at $(date -u +%H:%M:%S), rc=$rc"
    exit $rc
  } > "$LOG_DIR/build-elb-frontend.log" 2>&1 &
  PID_FRONTEND=$!

  {
    echo "[build-elb-terminal] starting at $(date -u +%H:%M:%S)"
    az acr build \
      --registry "$ACR_NAME" \
      --image "elb-terminal:${TAG}" \
      --file "terminal/Dockerfile.runtime" \
      --build-arg "TERMINAL_BASE_IMAGE=$TERMINAL_BASE_IMAGE_VAL" \
      "terminal/" \
      -o none
    rc=$?
    echo "[build-elb-terminal] finished at $(date -u +%H:%M:%S), rc=$rc"
    exit $rc
  } > "$LOG_DIR/build-elb-terminal.log" 2>&1 &
  PID_TERMINAL=$!

  ts "    elb-api:      pid=$PID_API"
  ts "    elb-frontend: pid=$PID_FRONTEND"
  ts "    elb-terminal: pid=$PID_TERMINAL"

  declare -A RUNNING=(
    ["elb-api"]=$PID_API
    ["elb-frontend"]=$PID_FRONTEND
    ["elb-terminal"]=$PID_TERMINAL
  )
  while [ ${#RUNNING[@]} -gt 0 ]; do
    sleep 15
    finished=()
    for name in "${!RUNNING[@]}"; do
      pid=${RUNNING["$name"]}
      if ! kill -0 "$pid" 2>/dev/null; then
        set +e
        wait "$pid"
        rc=$?
        set -e
        if [ "$rc" = "0" ]; then
          ts "    ✓ $name finished (rc=0)"
        else
          ts "    ✗ $name FAILED (rc=$rc) — see $LOG_DIR/build-$name.log"
          tail -30 "$LOG_DIR/build-$name.log" | sed "s/^/      [build-$name] /"
        fi
        finished+=("$name")
      fi
    done
    for name in "${finished[@]}"; do
      unset "RUNNING[$name]"
    done
    if [ ${#RUNNING[@]} -gt 0 ]; then
      ts "    waiting for: ${!RUNNING[*]}"
    fi
  done

  fail=0
  for name in elb-api elb-frontend elb-terminal; do
    if ! grep -q "rc=0$" "$LOG_DIR/build-$name.log" 2>/dev/null; then
      fail=1
      ts "✗ build $name did not produce rc=0"
    fi
  done
  if [ "$fail" = "1" ]; then
    ts "Aborting: at least one image build failed (ACR firewall will be restored on exit)."
    exit 1
  fi
  ts "==> All 3 images built and pushed"

  acr_restore_build_access "$ACR_NAME"
  trap - EXIT
fi  # end: if ! $NO_BUILD (all branch)

if $BUILD_ONLY; then
  ts "==> --build-only: skipping Container App PATCH. Built images:"
  ts "      $NEW_API"
  ts "      $NEW_FRONTEND"
  ts "      $NEW_TERMINAL"
  ts "==> Done. Tag was: $TAG"
  exit 0
fi

  # Pin mutable tags to their immutable digests so the PATCH actually changes
  # the Container App template and rolls a new revision. Without this,
  # `deploy all latest-main` is a silent no-op whenever the active revision
  # already references :latest-main (see resolve_image_digest).
  ts "==> Resolving image tags to digests for a deterministic revision roll"
  NEW_API="$(resolve_image_digest "$NEW_API")"
  NEW_FRONTEND="$(resolve_image_digest "$NEW_FRONTEND")"
  NEW_TERMINAL="$(resolve_image_digest "$NEW_TERMINAL")"
  ts "      api/worker/beat -> $NEW_API"
  ts "      frontend        -> $NEW_FRONTEND"
  ts "      terminal        -> $NEW_TERMINAL"

  # Sequential PATCHes -- see the long comment at the top of this block.
  # api / worker / beat share the elb-api image and are patched one at a
  # time to keep the read-modify-write semantics deterministic.
  declare -a PATCH_PLAN=(
    "api:$NEW_API"
    "worker:$NEW_API"
    "beat:$NEW_API"
    "frontend:$NEW_FRONTEND"
    "terminal:$NEW_TERMINAL"
  )
  for spec in "${PATCH_PLAN[@]}"; do
    tgt="${spec%%:*}"
    img="${spec#*:}"
    ts "==> Patching container '$tgt' on $CONTAINER_APP_NAME → $img"
    if [[ "$tgt" == "frontend" && "$NO_BUILD" != "true" ]]; then
      # Full deploy resolved VITE_* / API_CLIENT_ID on the host; mirror them
      # to the frontend runtime env so runtime-config.js stays in sync with
      # the image we just built.
      az containerapp update \
        --name "$CONTAINER_APP_NAME" \
        --resource-group "$AZURE_RESOURCE_GROUP" \
        --container-name "$tgt" \
        --image "$img" \
        --set-env-vars \
          "VITE_API_BASE_URL=$VITE_API_BASE_URL_VAL" \
          "VITE_AUTH_DEV_BYPASS=$VITE_AUTH_DEV_BYPASS_VAL" \
          "VITE_AZURE_REDIRECT_URI=$VITE_AZURE_REDIRECT_URI_VAL" \
          "VITE_AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL" \
          "VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL" \
          "VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB_VAL" \
          "VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS_VAL" \
          "VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL_VAL" \
          "API_CLIENT_ID=$API_CLIENT_ID_VAL" \
          "AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL" \
        -o none
    else
      # --no-build path (or non-frontend sidecar): swap image only and leave
      # the container's existing runtime env vars untouched — EXCEPT the
      # control-plane guard toggles, which we upsert from the shared JSON so a
      # Bicep guard-default change (e.g. ENFORCE_DASHBOARD_RBAC) actually lands
      # on a fast / GitHub-Actions deploy instead of waiting for a full
      # `azd provision`. The frontend's baked VITE_* values stay authoritative;
      # all other runtime env from the last full deploy / Bicep is preserved.
      mapfile -t _cp_pairs < <(control_plane_env_pairs "$tgt")
      if [[ ${#_cp_pairs[@]} -gt 0 ]]; then
        ts "    + applying ${#_cp_pairs[@]} control-plane guard env var(s) for '$tgt'"
        az containerapp update \
          --name "$CONTAINER_APP_NAME" \
          --resource-group "$AZURE_RESOURCE_GROUP" \
          --container-name "$tgt" \
          --image "$img" \
          --set-env-vars "${_cp_pairs[@]}" \
          -o none
      else
        # No guard toggles for this sidecar (terminal/redis), OR the shared
        # JSON is missing/moved. Log it so a vanished source file cannot
        # silently degrade an api/worker/beat PATCH to image-only and leave
        # a security guard (ENFORCE_DASHBOARD_RBAC) stale without warning.
        if [[ ! -f "$CONTROL_PLANE_ENV_FILE" ]]; then
          ts "    ! control-plane env file missing ($CONTROL_PLANE_ENV_FILE) — '$tgt' PATCHed image-only, guard env NOT applied"
        else
          ts "    (no control-plane guard env for '$tgt')"
        fi
        az containerapp update \
          --name "$CONTAINER_APP_NAME" \
          --resource-group "$AZURE_RESOURCE_GROUP" \
          --container-name "$tgt" \
          --image "$img" \
          -o none
      fi
    fi
  done

  ts "==> Latest revision:"
  az containerapp revision list \
    --name "$CONTAINER_APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --query "sort_by([], &properties.createdTime)[-1].{name:name, active:properties.active, state:properties.runningState, replicas:properties.replicas, created:properties.createdTime}" \
    -o table || true

  if $TAIL_LOGS; then
    ts "==> Tailing logs (Ctrl-C to exit) for container 'api'"
    az containerapp logs show \
      --name "$CONTAINER_APP_NAME" \
      --resource-group "$AZURE_RESOURCE_GROUP" \
      --container api \
      --follow \
      --tail 20
  fi

  ts "==> Done. Tag was: $TAG"
  ts "    To roll back all fast-deployed images, rerun: scripts/dev/quick-deploy.sh all <previous-tag>"
  exit 0
fi

case "$SIDECAR" in
  api|worker|beat) IMAGE_NAME="elb-api";       DOCKERFILE="api/Dockerfile";       BUILD_CTX="." ;;
  frontend)        IMAGE_NAME="elb-frontend";  DOCKERFILE="web/Dockerfile";       BUILD_CTX="." ;;
  terminal)        IMAGE_NAME="elb-terminal";  DOCKERFILE="terminal/Dockerfile.runtime";  BUILD_CTX="terminal/" ;;
  *) die "unknown sidecar '$SIDECAR' (expected: api|worker|beat|frontend|terminal|all)" ;;
esac

# Discover/align env from the active az login BEFORE validating env vars
# (see the matching block in the `all` branch above for the rationale).
assert_az_subscription_aligned

for v in AZURE_RESOURCE_GROUP ACR_NAME ACR_LOGIN_SERVER CONTAINER_APP_NAME; do
  [[ -n "${!v:-}" ]] || die "$v is unset and az-context discovery could not populate it (run: az login + verify the active sub has an elb-dashboard RG)"
done
confirm_deploy_target
preflight_permission_check
ensure_provider_registration_once
ensure_workspace_tags

NEW_IMAGE="${ACR_LOGIN_SERVER}/${IMAGE_NAME}:${TAG}"
API_CLIENT_ID_VAL="${VITE_AZURE_CLIENT_ID:-${API_CLIENT_ID:-}}"
AZURE_TENANT_ID_VAL="${VITE_AZURE_TENANT_ID:-${AZURE_TENANT_ID:-common}}"
if [[ "$AZURE_TENANT_ID_VAL" == "common" && -n "${AZURE_TENANT_ID:-}" ]]; then
  AZURE_TENANT_ID_VAL="$AZURE_TENANT_ID"
fi
VITE_AUTH_DEV_BYPASS_VAL="${VITE_AUTH_DEV_BYPASS:-false}"
VITE_API_BASE_URL_VAL="${VITE_API_BASE_URL:-}"
VITE_AZURE_REDIRECT_URI_VAL="${VITE_AZURE_REDIRECT_URI:-__RUNTIME__}"
VITE_FEATURE_CUSTOM_DB_VAL="${VITE_FEATURE_CUSTOM_DB:-true}"
VITE_FEATURE_LAB_TOOLS_VAL="${VITE_FEATURE_LAB_TOOLS:-true}"
VITE_FEATURE_TERMINAL_VAL="${VITE_FEATURE_TERMINAL:-true}"
if ! $NO_BUILD; then
  trap 'acr_restore_build_access "$ACR_NAME"' EXIT

  declare -a BUILD_ARGS=()
  if [[ "$SIDECAR" == "frontend" ]]; then
    [[ -n "$API_CLIENT_ID_VAL" ]] || die "API_CLIENT_ID/VITE_AZURE_CLIENT_ID is unset; set .env, web/.env.local, or azd env before deploying frontend"
    # Guard: a stale local-dev export (e.g. local-run.sh web) leaking
    # VITE_API_BASE_URL=http://localhost:... into this shell would bake the
    # loopback URL into the cloud frontend's runtime-config.js and break every
    # /api/* call from the browser. Force the operator to unset it first.
    if [[ -n "$VITE_API_BASE_URL_VAL" ]] && \
       [[ "$VITE_API_BASE_URL_VAL" =~ ^https?://(localhost|127\.|0\.0\.0\.0|\[::1\]) ]]; then
      die "VITE_API_BASE_URL='$VITE_API_BASE_URL_VAL' points at the local host — refusing to bake that into the cloud frontend. Run 'unset VITE_API_BASE_URL' (or export VITE_API_BASE_URL='') and retry."
    fi
    # Guard: VITE_AUTH_DEV_BYPASS=true makes the SPA skip MSAL while the api
    # sidecar still enforces bearer tokens — users hit a sea of 401s. The flag
    # is meant for local-debug only. Escape hatch (intentionally undocumented
    # in the help text): ELB_ALLOW_AUTH_BYPASS_IN_CLOUD=1.
    if [[ "$VITE_AUTH_DEV_BYPASS_VAL" == "true" && "${ELB_ALLOW_AUTH_BYPASS_IN_CLOUD:-0}" != "1" ]]; then
      die "VITE_AUTH_DEV_BYPASS=true — refusing to deploy a cloud frontend that skips MSAL while the api enforces bearer tokens. Run 'unset VITE_AUTH_DEV_BYPASS' (or export VITE_AUTH_DEV_BYPASS=false) and retry."
    fi
    # Guard: refuse to bake a frontend whose MSAL client id does not match the
    # target api sidecar's API_CLIENT_ID (the wrong-tenant sibling of the two
    # guards above). Escape hatch: ELB_ALLOW_MSAL_CLIENT_MISMATCH=1.
    assert_msal_client_matches_target "$API_CLIENT_ID_VAL"
    # Version stamp: ACR builds run without .git in context, so resolve on host.
    APP_VERSION_VAL="${APP_VERSION:-$(node -p "require('$REPO_ROOT/web/package.json').version" 2>/dev/null || echo 0.0.0)}"
    APP_BUILD_NUMBER_VAL="${APP_BUILD_NUMBER:-$(release_build_number)}"
    GIT_COMMIT_VAL="${GIT_COMMIT:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)}"
    BUILD_TIME_VAL="${BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
    BUILD_ARGS=(
      --build-arg "VITE_API_BASE_URL=$VITE_API_BASE_URL_VAL"
      --build-arg "VITE_AUTH_DEV_BYPASS=$VITE_AUTH_DEV_BYPASS_VAL"
      --build-arg "VITE_AZURE_REDIRECT_URI=$VITE_AZURE_REDIRECT_URI_VAL"
      --build-arg "VITE_AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL"
      --build-arg "VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL"
      --build-arg "VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB_VAL"
      --build-arg "VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS_VAL"
      --build-arg "VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL_VAL"
      --build-arg "APP_VERSION=$APP_VERSION_VAL"
      --build-arg "APP_BUILD_NUMBER=$APP_BUILD_NUMBER_VAL"
      --build-arg "GIT_COMMIT=$GIT_COMMIT_VAL"
      --build-arg "BUILD_TIME=$BUILD_TIME_VAL"
    )
  elif [[ "$SIDECAR" == "terminal" ]]; then
    BUILD_ARGS=(
      --build-arg "TERMINAL_BASE_IMAGE=$(terminal_base_image)"
    )
  elif [[ "$SIDECAR" == "api" || "$SIDECAR" == "worker" || "$SIDECAR" == "beat" ]]; then
    # Bake the release version into the api image so /api/health reports it
    # (api/__init__.py reads APP_VERSION). ACR builds run without .git in
    # context, so resolve the values on the host.
    APP_VERSION_VAL="${APP_VERSION:-$(node -p "require('$REPO_ROOT/web/package.json').version" 2>/dev/null || echo 0.0.0)}"
    GIT_COMMIT_VAL="${GIT_COMMIT:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)}"
    BUILD_TIME_VAL="${BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
    BUILD_ARGS=(
      --build-arg "APP_VERSION=$APP_VERSION_VAL"
      --build-arg "APP_GIT_COMMIT=$GIT_COMMIT_VAL"
      --build-arg "APP_BUILD_TIME=$BUILD_TIME_VAL"
    )
  fi

  ts "==> Building $IMAGE_NAME:$TAG via ACR (no local Docker)"
  ts "    dockerfile=$DOCKERFILE  context=$BUILD_CTX"
  acr_ensure_build_access "$ACR_NAME"
  if [[ "$SIDECAR" == "terminal" ]]; then
    TERMINAL_BASE_REBUILD="$REBUILD_TERMINAL_BASE" ensure_terminal_base_image
  fi
  az acr build \
    --registry "$ACR_NAME" \
    --image "${IMAGE_NAME}:${TAG}" \
    --file "$DOCKERFILE" \
    "${BUILD_ARGS[@]}" \
    "$BUILD_CTX" \
    -o none

  acr_restore_build_access "$ACR_NAME"
  trap - EXIT

  ts "==> Build complete: $NEW_IMAGE"
else
  ts "==> --no-build: skipping ACR build; expecting tag '$TAG' to already exist for $IMAGE_NAME"
fi

if $BUILD_ONLY; then
  ts "==> --build-only: skipping Container App PATCH for $SIDECAR ($NEW_IMAGE)"
  ts "==> Done. Tag was: $TAG"
  exit 0
fi

# --------------------------------------------------------------------------
# api / worker / beat all share the elb-api image. When the user runs
# `quick-deploy.sh api` we ALSO bump worker + beat so they pick up the
# new task code; otherwise the worker would keep running stale logic
# while the api fronts new logic — exactly the scenario that caused the
# Celery routing trap to look like an infra bug last week.
# --------------------------------------------------------------------------
declare -a TARGETS
case "$SIDECAR" in
  api)              TARGETS=(api worker beat) ;;
  worker)           TARGETS=(worker) ;;
  beat)             TARGETS=(beat) ;;
  frontend)         TARGETS=(frontend) ;;
  terminal)         TARGETS=(terminal) ;;
esac

# Pin the mutable tag to its digest so the PATCH rolls a new revision even
# when the active revision already references the same tag (see
# resolve_image_digest in the helpers block).
NEW_IMAGE="$(resolve_image_digest "$NEW_IMAGE")"

for tgt in "${TARGETS[@]}"; do
  ts "==> Patching container '$tgt' on $CONTAINER_APP_NAME → $NEW_IMAGE"
  if [[ "$tgt" == "frontend" && "$NO_BUILD" != "true" ]]; then
    az containerapp update \
      --name "$CONTAINER_APP_NAME" \
      --resource-group "$AZURE_RESOURCE_GROUP" \
      --container-name "$tgt" \
      --image "$NEW_IMAGE" \
      --set-env-vars \
        "VITE_API_BASE_URL=$VITE_API_BASE_URL_VAL" \
        "VITE_AUTH_DEV_BYPASS=$VITE_AUTH_DEV_BYPASS_VAL" \
        "VITE_AZURE_REDIRECT_URI=$VITE_AZURE_REDIRECT_URI_VAL" \
        "VITE_AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL" \
        "VITE_AZURE_CLIENT_ID=$API_CLIENT_ID_VAL" \
        "VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB_VAL" \
        "VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS_VAL" \
        "VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL_VAL" \
        "API_CLIENT_ID=$API_CLIENT_ID_VAL" \
        "AZURE_TENANT_ID=$AZURE_TENANT_ID_VAL" \
      -o none
  else
    # Non-frontend sidecar: swap image and upsert the control-plane guard
    # toggles from the shared JSON (see the helper near the top of this file).
    # This keeps a fast single-sidecar deploy in sync with a Bicep guard
    # default instead of silently leaving the live env stale.
    mapfile -t _cp_pairs < <(control_plane_env_pairs "$tgt")
    if [[ ${#_cp_pairs[@]} -gt 0 ]]; then
      ts "    + applying ${#_cp_pairs[@]} control-plane guard env var(s) for '$tgt'"
      az containerapp update \
        --name "$CONTAINER_APP_NAME" \
        --resource-group "$AZURE_RESOURCE_GROUP" \
        --container-name "$tgt" \
        --image "$NEW_IMAGE" \
        --set-env-vars "${_cp_pairs[@]}" \
        -o none
    else
      # No guard toggles for this sidecar, OR the shared JSON is missing.
      # Log it so a vanished source cannot silently leave a security guard
      # stale on an api/worker/beat PATCH (see the `all` path for rationale).
      if [[ ! -f "$CONTROL_PLANE_ENV_FILE" ]]; then
        ts "    ! control-plane env file missing ($CONTROL_PLANE_ENV_FILE) — '$tgt' PATCHed image-only, guard env NOT applied"
      else
        ts "    (no control-plane guard env for '$tgt')"
      fi
      az containerapp update \
        --name "$CONTAINER_APP_NAME" \
        --resource-group "$AZURE_RESOURCE_GROUP" \
        --container-name "$tgt" \
        --image "$NEW_IMAGE" \
        -o none
    fi
  fi
done

ts "==> Latest revision:"
az containerapp revision list \
  --name "$CONTAINER_APP_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --query "sort_by([], &properties.createdTime)[-1].{name:name, active:properties.active, state:properties.runningState, replicas:properties.replicas, created:properties.createdTime}" \
  -o table || true

if $TAIL_LOGS; then
  ts "==> Tailing logs (Ctrl-C to exit) for container '${TARGETS[0]}'"
  az containerapp logs show \
    --name "$CONTAINER_APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --container "${TARGETS[0]}" \
    --follow \
    --tail 20
fi

# --------------------------------------------------------------------------
# Optional Service Bus integration RBAC. The namespace is normally chosen at
# runtime from Settings (so quick-deploy cannot know it), but when the operator
# exports SERVICEBUS_NAMESPACE (+ optional SERVICEBUS_NAMESPACE_RG) we grant the
# shared managed identity the two data-plane roles it needs to drain requests
# and publish completions over Entra. Idempotent: an existing assignment is a
# no-op. SAS-mode / cross-tenant namespaces are skipped (Entra cannot reach
# them) — those use a connection-string secret instead. Never narrows a role
# (charter §12a Rule 1: additive only).
# --------------------------------------------------------------------------
ensure_service_bus_rbac() {
  local ns="${SERVICEBUS_NAMESPACE:-}"
  [[ -n "$ns" ]] || { ts "    (SERVICEBUS_NAMESPACE unset — skipping Service Bus RBAC grant)"; return 0; }
  local ns_rg="${SERVICEBUS_NAMESPACE_RG:-$AZURE_RESOURCE_GROUP}"
  local mi_principal
  mi_principal="$(az identity list \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --query "[?starts_with(name,'id-elb-dashboard')].principalId | [0]" \
    -o tsv 2>/dev/null || true)"
  if [[ -z "$mi_principal" || "$mi_principal" == "None" ]]; then
    ts "    ! could not resolve shared MI principal in '$AZURE_RESOURCE_GROUP' — skipping Service Bus RBAC"
    return 0
  fi
  local ns_id
  ns_id="$(az servicebus namespace show \
    --name "$ns" --resource-group "$ns_rg" --query id -o tsv 2>/dev/null || true)"
  if [[ -z "$ns_id" ]]; then
    ts "    ! Service Bus namespace '$ns' not found in '$ns_rg' — skipping RBAC grant"
    return 0
  fi
  local role
  for role in "Azure Service Bus Data Sender" "Azure Service Bus Data Receiver"; do
    if az role assignment create \
      --assignee-object-id "$mi_principal" --assignee-principal-type ServicePrincipal \
      --role "$role" --scope "$ns_id" --only-show-errors >/dev/null 2>&1; then
      ts "    + granted '$role' to shared MI on $ns"
    else
      ts "    (role '$role' already present or grant skipped for $ns)"
    fi
  done
}

# Only relevant for the api/worker/beat image (which runs the integration).
case "$SIDECAR" in
  api|worker|beat) ensure_service_bus_rbac ;;
esac

ts "==> Done. Tag was: $TAG"
ts "    To roll back: scripts/dev/quick-deploy.sh $SIDECAR <previous-tag>"
