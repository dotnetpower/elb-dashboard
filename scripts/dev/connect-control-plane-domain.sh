#!/usr/bin/env bash
# connect-control-plane-domain.sh — bind a custom domain to the dashboard
# Container App (the "real connection config" the Settings → Control plane domain
# value does NOT perform on its own; that value is only the webhook CONTROL_PLANE_URL).
#
# What it does (idempotent):
#   1. Resolves the Container App FQDN, the environment static IP, and the
#      customDomainVerificationId.
#   2. Picks apex vs sub-domain mode from the Azure DNS zone layout and upserts:
#        - ownership:  TXT  asuid[.<label>]            -> verificationId
#        - routing:    A @  -> env static IP  (apex)   OR
#                      CNAME <label> -> app FQDN (sub-domain)
#   3. Checks PUBLIC DNS. If the domain is not resolvable yet (the parent zone's
#      registrar nameservers are not pointed at Azure DNS) it prints the exact
#      registrar nameservers to set and STOPS before the (doomed) bind — a
#      managed certificate cannot be issued until public DNS resolves.
#   4. Binds the hostname with a free Azure-managed certificate
#      (az containerapp hostname add + bind --validation-method).
#
# This script never modifies the Container App's ingress beyond *adding* the
# custom domain (the auto-generated *.azurecontainerapps.io FQDN keeps working),
# needs no new managed-identity RBAC, and is safe to re-run.
#
# Usage:
#   scripts/dev/connect-control-plane-domain.sh --domain dashboard.elasticblast.com
#   scripts/dev/connect-control-plane-domain.sh --domain dashboard.example.com --dry-run
#   scripts/dev/connect-control-plane-domain.sh --domain api.example.com --force   # skip the public-DNS gate
#
# Env defaults (override with flags or export):
#   CONTAINER_APP_NAME   (default: ca-elb-dashboard)
#   AZURE_RESOURCE_GROUP (default: rg-elb-dashboard)
#   AZURE_SUBSCRIPTION_ID / SUBSCRIPTION (default: current az account)
#   DNS_ZONE / DNS_ZONE_RG (default: auto-detected from the Azure DNS zones)

set -Eeuo pipefail

DOMAIN=""
APP="${CONTAINER_APP_NAME:-ca-elb-dashboard}"
RG="${AZURE_RESOURCE_GROUP:-rg-elb-dashboard}"
SUB="${AZURE_SUBSCRIPTION_ID:-${SUBSCRIPTION:-}}"
DNS_ZONE="${DNS_ZONE:-}"
DNS_ZONE_RG="${DNS_ZONE_RG:-}"
VALIDATION=""
DRY_RUN=0
FORCE=0

die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
log() { printf '\033[36m==>\033[0m %s\n' "$*"; }
run() { if [[ "$DRY_RUN" == 1 ]]; then printf '  [dry-run] %s\n' "$*"; else eval "$@"; fi; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --app) APP="$2"; shift 2 ;;
    --resource-group|-g) RG="$2"; shift 2 ;;
    --subscription) SUB="$2"; shift 2 ;;
    --zone) DNS_ZONE="$2"; shift 2 ;;
    --zone-rg) DNS_ZONE_RG="$2"; shift 2 ;;
    --validation) VALIDATION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$DOMAIN" ]] || die "--domain is required (e.g. --domain dashboard.elasticblast.com)"
command -v az >/dev/null || die "az CLI not found"

# The azure-cli containerapp extension only accepts global args (e.g.
# --subscription) AFTER the command group, so append them via this wrapper
# rather than as an `az --subscription …` prefix.
AZ=(az)
SUBARG=()
[[ -n "$SUB" ]] && SUBARG=(--subscription "$SUB")
azc() { "${AZ[@]}" "$@" "${SUBARG[@]}"; }

log "Container App: $APP (rg=$RG)"
APP_JSON="$(azc containerapp show -n "$APP" -g "$RG" \
  --query "{fqdn:properties.configuration.ingress.fqdn, envId:properties.managedEnvironmentId, verif:properties.customDomainVerificationId}" -o json)" \
  || die "could not read Container App $APP in $RG"
APP_FQDN="$(printf '%s' "$APP_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["fqdn"])')"
ENV_ID="$(printf '%s' "$APP_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["envId"])')"
VERIF="$(printf '%s' "$APP_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["verif"])')"
ENV_NAME="$(basename "$ENV_ID")"
STATIC_IP="$(azc containerapp env show --ids "$ENV_ID" --query "properties.staticIp" -o tsv)"
log "app FQDN: $APP_FQDN"
log "env: $ENV_NAME  static IP: $STATIC_IP"
log "verification id: ${VERIF:0:12}…"

# ── Resolve the Azure DNS zone for the domain ──────────────────────────────
if [[ -z "$DNS_ZONE" ]]; then
  # Exact-zone (apex) match first, then the longest parent suffix.
  mapfile -t ZONES < <(azc network dns zone list --query "[].{n:name,rg:resourceGroup}" -o tsv 2>/dev/null)
  best=""
  for row in "${ZONES[@]}"; do
    zname="${row%%$'\t'*}"; zrg="${row#*$'\t'}"
    if [[ "$DOMAIN" == "$zname" || "$DOMAIN" == *".$zname" ]]; then
      if [[ ${#zname} -gt ${#best} ]]; then best="$zname"; DNS_ZONE="$zname"; DNS_ZONE_RG="$zrg"; fi
    fi
  done
fi
[[ -n "$DNS_ZONE" ]] || die "no Azure DNS zone found covering $DOMAIN (pass --zone/--zone-rg)"
[[ -n "$DNS_ZONE_RG" ]] || DNS_ZONE_RG="$RG"
log "DNS zone: $DNS_ZONE (rg=$DNS_ZONE_RG)"

# Record names relative to the zone. Apex when DOMAIN == zone.
if [[ "$DOMAIN" == "$DNS_ZONE" ]]; then
  APEX=1; LABEL="@"; ASUID_NAME="asuid"; DEF_VALIDATION="HTTP"
else
  APEX=0; LABEL="${DOMAIN%.$DNS_ZONE}"; ASUID_NAME="asuid.${LABEL}"; DEF_VALIDATION="CNAME"
fi
VALIDATION="${VALIDATION:-$DEF_VALIDATION}"

# ── Upsert DNS records ─────────────────────────────────────────────────────
log "Upserting ownership TXT $ASUID_NAME -> verificationId"
run "azc network dns record-set txt add-record -g '$DNS_ZONE_RG' -z '$DNS_ZONE' --record-set-name '$ASUID_NAME' --value '$VERIF' -o none" || true
if [[ "$APEX" == 1 ]]; then
  log "Upserting routing A @ -> $STATIC_IP"
  run "azc network dns record-set a add-record -g '$DNS_ZONE_RG' -z '$DNS_ZONE' --record-set-name '@' --ipv4-address '$STATIC_IP' -o none" || true
else
  log "Upserting routing CNAME $LABEL -> $APP_FQDN"
  run "azc network dns record-set cname set-record -g '$DNS_ZONE_RG' -z '$DNS_ZONE' --record-set-name '$LABEL' --cname '$APP_FQDN' -o none" || true
fi

# ── Public-DNS gate ────────────────────────────────────────────────────────
PUB="$(dig +short "$DOMAIN" @8.8.8.8 2>/dev/null | head -1 || true)"
if [[ -z "$PUB" && "$FORCE" != 1 ]]; then
  ZONE_NS="$(azc network dns zone show -g "$DNS_ZONE_RG" -n "$DNS_ZONE" --query "nameServers" -o tsv 2>/dev/null | tr '\n' ' ')"
  PARENT="${DNS_ZONE#*.}"
  cat >&2 <<EOF

DNS not public yet — STOPPING before the certificate bind.
  '$DOMAIN' does not resolve on public DNS (dig @8.8.8.8 returned nothing).
  Azure DNS zone records are in place, but the domain is not delegated to Azure.

  Fix at the REGISTRAR where '${PARENT:-the parent domain}' is registered — set its
  nameservers to this zone's Azure nameservers:
$(for n in $ZONE_NS; do echo "    $n"; done)

  (Creating an Azure DNS zone does NOT delegate the domain; the registrar step is
  separate and is the only thing that makes the zone visible to the internet.)

  A managed certificate cannot be issued until '$DOMAIN' resolves publicly. After
  the registrar change propagates (verify with: dig +short $DOMAIN @8.8.8.8),
  re-run this script to complete the bind. Use --force to attempt the bind anyway.
EOF
  exit 2
fi
[[ "$FORCE" == 1 ]] && log "--force set: skipping the public-DNS gate"

# ── Bind the hostname with a managed certificate ───────────────────────────
log "Adding hostname $DOMAIN to $APP"
run "azc containerapp hostname add -n '$APP' -g '$RG' --hostname '$DOMAIN' -o none" || true
log "Binding $DOMAIN with a managed certificate (validation: $VALIDATION)"
run "azc containerapp hostname bind -n '$APP' -g '$RG' --hostname '$DOMAIN' --environment '$ENV_ID' --validation-method '$VALIDATION' -o none"

log "Done. Current custom domains on $APP:"
run "azc containerapp hostname list -n '$APP' -g '$RG' --query '[].name' -o tsv"
cat <<EOF

Next:
  - Managed certificate issuance is asynchronous (a few minutes). Check with:
      az containerapp hostname list -n '$APP' -g '$RG' -o table
  - Once the binding shows a certificate, browse https://$DOMAIN
  - Optionally set Settings → Control plane domain to https://$DOMAIN so the
    sibling webhooks back to the branded host (CONTROL_PLANE_URL).
EOF
