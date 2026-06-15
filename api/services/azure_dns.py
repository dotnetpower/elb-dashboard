"""Azure DNS record management for the OpenAPI public-HTTPS custom domain.

Resolve which Azure DNS zone owns a custom domain (e.g. ``api.elasticblast.com``)
and idempotently upsert the record that points it at the cluster's public
ingress, so the Let's Encrypt HTTP-01 challenge can validate the custom domain.

Responsibility: Locate the parent DNS zone for a custom FQDN within the
    subscription and create/update the routing record (CNAME -> cloudapp FQDN for
    a sub-domain, A -> LB IP for an apex). Every operation is best-effort: a
    missing zone or an ``AuthorizationFailed`` returns a structured result dict
    instead of raising, so the public-HTTPS pipeline degrades to an operator
    "create this record manually" instruction rather than aborting.
Edit boundaries: Reusable Azure DNS domain logic only. The Celery pipeline in
    ``api.tasks.openapi.public_https`` calls ``ensure_public_dns_record``; HTTP
    shaping stays in the route layer. The only Azure SDK entry point is
    ``api.services.azure_clients.dns_client``.
Key entry points: ``split_custom_domain``, ``find_zone_for_fqdn``,
    ``ensure_public_dns_record``.
Risky contracts: An apex record (FQDN == zone name) cannot be a CNAME (RFC 1034),
    so the apex path writes an A record to ``lb_ip``; a sub-domain writes a CNAME
    to the stable ``cloudapp_fqdn`` (survives LB IP churn). ``find_zone_for_fqdn``
    returns the *longest* matching zone suffix so ``a.b.example.com`` binds to a
    ``b.example.com`` zone before ``example.com``. Never raises on an Azure fault
    — the caller relies on the returned ``status`` to decide manual-vs-auto.
Validation: ``uv run pytest -q api/tests/test_azure_dns.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from azure.core.exceptions import HttpResponseError

from api.services import get_credential
from api.services.azure_clients import dns_client

LOGGER = logging.getLogger(__name__)

_DEFAULT_TTL = 300

# Result status values (kept stable — the SPA + tests assert on them).
STATUS_CREATED = "created"
STATUS_NO_ZONE = "no_zone"
STATUS_FORBIDDEN = "forbidden"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class ZoneMatch:
    """A resolved DNS zone and the relative record name within it."""

    zone_name: str
    resource_group: str
    record_name: str  # "@" for the apex, else the relative label(s).


def split_custom_domain(fqdn: str) -> str:
    """Normalise a custom-domain input to a bare lower-case FQDN.

    Strips a scheme / trailing slash / surrounding whitespace and a single
    trailing dot. Returns ``""`` for empty input. Does NOT validate that the
    FQDN is well-formed beyond trimming — :func:`find_zone_for_fqdn` is the
    authority on whether a hosted zone actually owns it.
    """
    value = (fqdn or "").strip().lower()
    if not value:
        return ""
    for scheme in ("https://", "http://"):
        if value.startswith(scheme):
            value = value[len(scheme) :]
            break
    value = value.split("/", 1)[0].strip()
    return value.rstrip(".")


def _resource_group_from_zone_id(zone_id: str) -> str:
    """Extract the resource group from a DNS zone ARM id (case-insensitive)."""
    parts = (zone_id or "").split("/")
    for index, token in enumerate(parts):
        if token.lower() == "resourcegroups" and index + 1 < len(parts):
            return parts[index + 1]
    return ""


def _relative_record_name(fqdn: str, zone_name: str) -> str:
    """Return the record name relative to the zone (``@`` for the apex)."""
    if fqdn == zone_name:
        return "@"
    suffix = f".{zone_name}"
    if fqdn.endswith(suffix):
        return fqdn[: -len(suffix)]
    # Should not happen — caller already matched the suffix — but stay safe.
    return fqdn


def find_zone_for_fqdn(subscription_id: str, fqdn: str) -> ZoneMatch | None:
    """Return the longest-matching hosted zone for ``fqdn``, or ``None``.

    Enumerates the subscription's DNS zones and picks the one whose name is the
    longest suffix of ``fqdn`` (so ``a.b.example.com`` prefers a ``b.example.com``
    zone over ``example.com``). Returns ``None`` when no zone owns the FQDN or the
    list call fails (best-effort — the caller degrades to manual instructions).
    """
    fqdn = split_custom_domain(fqdn)
    if not fqdn:
        return None
    try:
        cred = get_credential()
        client = dns_client(cred, subscription_id)
        best: ZoneMatch | None = None
        best_len = -1
        for zone in client.zones.list():
            zone_name = (zone.name or "").strip().lower().rstrip(".")
            if not zone_name:
                continue
            is_apex = fqdn == zone_name
            is_subdomain = fqdn.endswith(f".{zone_name}")
            if not (is_apex or is_subdomain):
                continue
            if len(zone_name) > best_len:
                best_len = len(zone_name)
                best = ZoneMatch(
                    zone_name=zone_name,
                    resource_group=_resource_group_from_zone_id(zone.id or ""),
                    record_name=_relative_record_name(fqdn, zone_name),
                )
        return best
    except HttpResponseError as exc:
        LOGGER.warning(
            "dns zone lookup failed for fqdn=%s: %s",
            fqdn,
            getattr(exc, "status_code", "?"),
        )
        return None
    except Exception:
        LOGGER.debug("dns zone lookup raised", exc_info=True)
        return None


def ensure_public_dns_record(
    *,
    subscription_id: str,
    custom_domain: str,
    cloudapp_fqdn: str,
    lb_ip: str = "",
    ttl: int = _DEFAULT_TTL,
) -> dict[str, Any]:
    """Idempotently point ``custom_domain`` at the cluster's public ingress.

    Writes a CNAME (sub-domain → ``cloudapp_fqdn``, the stable Azure-assigned
    name) or an A record (apex → ``lb_ip``). Best-effort: returns a structured
    result dict and never raises, so a 403 / missing zone degrades the
    public-HTTPS pipeline to "create this record manually" rather than aborting
    cert issuance (the cert still issues once the operator adds the record).

    Returns a dict with ``status`` in {``created`` / ``no_zone`` / ``forbidden``
    / ``error``} plus the fields the SPA / manual instruction needs
    (``record_type``, ``record_name``, ``zone_name``, ``target``, ``detail``).
    """
    fqdn = split_custom_domain(custom_domain)
    cloudapp = split_custom_domain(cloudapp_fqdn)
    base = {
        "custom_domain": fqdn,
        "record_type": "",
        "record_name": "",
        "zone_name": "",
        "target": "",
    }
    if not fqdn:
        return {**base, "status": STATUS_ERROR, "detail": "empty custom_domain"}

    match = find_zone_for_fqdn(subscription_id, fqdn)
    if match is None:
        return {
            **base,
            "status": STATUS_NO_ZONE,
            "detail": (
                f"No Azure DNS zone in this subscription owns {fqdn}. Create the "
                f"record manually: {fqdn} CNAME {cloudapp or '<cluster public FQDN>'}"
            ),
        }

    is_apex = match.record_name == "@"
    if is_apex and not lb_ip:
        return {
            **base,
            "zone_name": match.zone_name,
            "record_name": "@",
            "status": STATUS_ERROR,
            "detail": (
                "Apex domain requires an A record but the ingress LB IP is not "
                "available yet."
            ),
        }
    record_type = "A" if is_apex else "CNAME"
    target = lb_ip if is_apex else cloudapp
    if not target:
        return {
            **base,
            "zone_name": match.zone_name,
            "record_name": match.record_name,
            "record_type": record_type,
            "status": STATUS_ERROR,
            "detail": "No target available for the DNS record.",
        }

    if is_apex:
        parameters = {"ttl": ttl, "a_records": [{"ipv4_address": lb_ip}]}
    else:
        parameters = {"ttl": ttl, "cname_record": {"cname": cloudapp}}

    result = {
        **base,
        "zone_name": match.zone_name,
        "record_name": match.record_name,
        "record_type": record_type,
        "target": target,
    }
    try:
        cred = get_credential()
        client = dns_client(cred, subscription_id)
        client.record_sets.create_or_update(
            match.resource_group,
            match.zone_name,
            match.record_name,
            record_type,
            parameters,
        )
        LOGGER.info(
            "dns record upserted %s %s in zone=%s -> %s",
            record_type,
            match.record_name,
            match.zone_name,
            target,
        )
        return {**result, "status": STATUS_CREATED}
    except HttpResponseError as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code in (401, 403):
            return {
                **result,
                "status": STATUS_FORBIDDEN,
                "detail": (
                    "The managed identity lacks DNS Zone Contributor on "
                    f"{match.zone_name}. Create the record manually: "
                    f"{fqdn} {record_type} {target}"
                ),
            }
        LOGGER.warning("dns record upsert failed status=%s", status_code)
        return {
            **result,
            "status": STATUS_ERROR,
            "detail": f"DNS upsert failed (status {status_code}).",
        }
    except Exception:
        LOGGER.debug("dns record upsert raised", exc_info=True)
        return {**result, "status": STATUS_ERROR, "detail": "DNS upsert error."}
