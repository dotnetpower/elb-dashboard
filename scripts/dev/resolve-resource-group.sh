#!/usr/bin/env bash
# Resolve the platform resource group before azd provision starts.

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: scripts/dev/resolve-resource-group.sh [--subscription <id-or-name>] [--environment <azd-env>]

If rg-elb-dashboard already exists and contains resources, ask whether to:
  1. delete it and continue with rg-elb-dashboard, or
  2. use the next numbered group such as rg-elb-dashboard-01.

Environment overrides:
  ELB_EXISTING_RG_ACTION      delete | number | abort
  ELB_RESOURCE_NAME_SUFFIX    Optional input suffix such as -01
  ELB_RESOURCE_NAME_SLOT      Internal azd-safe slot such as slot01

USAGE
}

subscription_arg=()
environment_name="${AZURE_ENV_NAME:-${AZD_ENV_NAME:-}}"
repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subscription)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --subscription requires a subscription id or name." >&2
        exit 1
      fi
      subscription_arg=(--subscription "$2")
      shift 2
      ;;
    --environment)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --environment requires an azd environment name." >&2
        exit 1
      fi
      environment_name="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

base_rg="rg-elb-dashboard"
suffix="${ELB_RESOURCE_NAME_SUFFIX:-}"
slot="${ELB_RESOURCE_NAME_SLOT:-}"
action="${ELB_EXISTING_RG_ACTION:-}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: $1 is required." >&2
    exit 1
  }
}

need az

if [[ -n "$suffix" && ! "$suffix" =~ ^-[0-9][0-9]$ ]]; then
  echo "ERROR: ELB_RESOURCE_NAME_SUFFIX must be empty or look like -01." >&2
  exit 1
fi

if [[ -n "$slot" && ! "$slot" =~ ^slot[0-9][0-9]$ ]]; then
  echo "ERROR: ELB_RESOURCE_NAME_SLOT must be empty or look like slot01." >&2
  exit 1
fi

if [[ -n "$action" && ! "$action" =~ ^(delete|number|abort)$ ]]; then
  echo "ERROR: ELB_EXISTING_RG_ACTION must be delete, number, or abort." >&2
  exit 1
fi

if [[ -n "$suffix" ]]; then
  slot="slot${suffix#-}"
elif [[ -n "$slot" ]]; then
  suffix="-${slot#slot}"
fi

set_azd_value() {
  local key="$1"
  local value="$2"
  local env_file tmp escaped
  if [[ -z "$environment_name" ]]; then
    return 0
  fi

  env_file="${repo_root}/.azure/${environment_name}/.env"
  if [[ -f "$env_file" ]]; then
    escaped="${value//\\/\\\\}"
    escaped="${escaped//\"/\\\"}"
    tmp="$(mktemp)"
    if grep -q "^${key}=" "$env_file"; then
      awk -v key="$key" -v line="${key}=\"${escaped}\"" '
        index($0, key "=") == 1 { print line; next }
        { print }
      ' "$env_file" > "$tmp"
    else
      cp "$env_file" "$tmp"
      printf '%s="%s"\n' "$key" "$escaped" >> "$tmp"
    fi
    mv "$tmp" "$env_file"
  elif command -v azd >/dev/null 2>&1; then
    azd env set --environment "$environment_name" "$key" -- "$value" >/dev/null
  fi
}

set_azd_slot() {
  local value="$1"
  set_azd_value ELB_RESOURCE_NAME_SLOT "$value"
  set_azd_value ELB_RESOURCE_NAME_SUFFIX ""
}

rg_exists() {
  [[ "$(az group exists "${subscription_arg[@]}" --name "$1" -o tsv 2>/dev/null || echo false)" == "true" ]]
}

resource_count() {
  az resource list "${subscription_arg[@]}" --resource-group "$1" --query 'length(@)' -o tsv 2>/dev/null || echo 0
}

print_sample_resources() {
  az resource list "${subscription_arg[@]}" --resource-group "$1" \
    --query '[0:10].{name:name,type:type}' -o table 2>/dev/null || true
}

next_numbered_suffix() {
  local candidate count n
  for n in $(seq 1 99); do
    candidate="$(printf -- '-%02d' "$n")"
    if ! rg_exists "${base_rg}${candidate}"; then
      printf '%s' "$candidate"
      return 0
    fi
    count="$(resource_count "${base_rg}${candidate}")"
    if [[ "$count" == "0" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

target_rg="${base_rg}${suffix}"
if [[ -n "$suffix" ]]; then
  echo "==> Resource group suffix already selected: $suffix"
  echo "==> Target resource group: $target_rg"
  set_azd_slot "$slot"
  exit 0
fi

if ! rg_exists "$base_rg"; then
  echo "==> Target resource group is available: $base_rg"
  set_azd_slot ""
  exit 0
fi

count="$(resource_count "$base_rg")"
if [[ "$count" == "0" ]]; then
  echo "==> Target resource group exists but is empty; reusing: $base_rg"
  set_azd_slot ""
  exit 0
fi

cat >&2 <<EOF

WARNING: Resource group '$base_rg' already exists and contains $count resources.

Existing resources in this group may be from an older elb-dashboard deployment.
Azure cannot rename Container Apps, Storage accounts, or ACRs, so continuing with
the new naming convention may create side-by-side resources unless you choose a
clean target.

Sample resources:
EOF
print_sample_resources "$base_rg" >&2

if [[ -z "$action" ]]; then
  if [[ -t 0 ]]; then
    cat >&2 <<'EOF'

Choose how to continue:
  d) Delete rg-elb-dashboard, wait for deletion, then deploy fresh with rg-elb-dashboard.
  n) Keep it and use the next numbered resource group, for example rg-elb-dashboard-01.
  a) Abort.
EOF
    read -r -p "Selection [d/n/a]: " reply
    case "${reply,,}" in
      d|delete) action="delete" ;;
      n|number) action="number" ;;
      a|abort|"") action="abort" ;;
      *)
        echo "ERROR: unknown selection: $reply" >&2
        exit 1
        ;;
    esac
  else
    action="abort"
  fi
fi

case "$action" in
  delete)
    echo "==> Deleting $base_rg before deployment continues. This can take several minutes."
    az group delete "${subscription_arg[@]}" --name "$base_rg" --yes --only-show-errors
    set_azd_slot ""
    echo "==> Deleted $base_rg. Continuing with target resource group: $base_rg"
    ;;
  number)
    suffix="$(next_numbered_suffix)" || {
      echo "ERROR: could not find an available numbered resource group from ${base_rg}-01 to ${base_rg}-99." >&2
      exit 1
    }
    slot="slot${suffix#-}"
    set_azd_slot "$slot"
    echo "==> Keeping $base_rg. Continuing with numbered resource group: ${base_rg}${suffix}"
    ;;
  abort)
    cat >&2 <<EOF
Aborted because '$base_rg' already contains resources.

Set ELB_EXISTING_RG_ACTION=delete to delete it automatically, or
ELB_EXISTING_RG_ACTION=number to use the next numbered group.
EOF
    exit 1
    ;;
esac