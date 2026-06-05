"""Private VNet peering and probe settings route.

Responsibility: Peer a target VNet into the selected AKS cluster VNet and
probe the private OpenAPI endpoint so the operator can verify reachability
from the remote network. Also expose an explicit "apply NSG rule" action
that lets the operator unblock the probe in one click when their RBAC
permits writing to the target subnet's NSG.
Edit boundaries: HTTP validation + response shaping only. All Azure SDK work
is delegated to `api.tasks.azure.peering` and `api.tasks.azure.peering_nsg`;
the guaranteed terminal-event audit lifecycle lives in
`api.services.peering_nsg_audit`.
Key entry points: `peer_vnet`, `list_existing_peerings`, `apply_peering_nsg_rule`.
Risky contracts: The helper already absorbs per-peering failures into the
returned payload. This route only turns hard helper failures into a stable
502 so the SPA can show a recoverable error instead of a raw 500. The
``apply-nsg-rule`` endpoint never accepts an operator-supplied source CIDR
or destination — both are derived from the resolved VNet pair. Port input
is clamped to the {80, 443} allowlist before it can reach ARM.
Validation: `uv run pytest -q api/tests/test_settings_vnet_peering.py
api/tests/test_peering_nsg.py`.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.auth import CallerIdentity, require_caller
from api.services.peering_nsg_audit import (
    audit_session as _audit_session,
)
from api.services.peering_nsg_audit import (
    record_audit_event as _record_audit_event,
)
from api.services.peering_nsg_audit import (
    record_audit_started as _record_audit_started,
)
from api.services.sanitise import redact_oid, sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter()


_RE_SUB = re.compile(r"^[0-9a-fA-F-]{36}$")
_RE_RG = re.compile(r"^[-\w._()]{1,90}$")
_RE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{1,254}$")


def _require(value: Any, pattern: re.Pattern[str], label: str) -> str:
    text = (value or "").strip()
    if not isinstance(value, str) or not pattern.match(text):
        raise HTTPException(400, f"invalid {label}")
    return text


@router.get("/vnet-peering/existing")
def list_existing_peerings(
    subscription_id: str = Query(...),
    resource_group: str = Query(...),
    cluster_name: str = Query(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """List the peerings already present on the cluster's AKS VNet (read-only).

    Best-effort: the helper never raises on an Azure fault, so this route only
    converts a hard helper failure into a stable 502. Routine RBAC denials /
    skip cases are surfaced inside the 200 payload (``error`` / ``skipped``) so
    the Settings panel can render an explanatory banner instead of breaking.
    """
    sub = _require(subscription_id, _RE_SUB, "subscription_id")
    rg = _require(resource_group, _RE_RG, "resource_group")
    cluster = _require(cluster_name, _RE_NAME, "cluster_name")

    LOGGER.info(
        "settings/vnet-peering/existing requested cluster=%s rg=%s caller_oid=%s",
        cluster,
        rg,
        redact_oid(caller.object_id),
    )

    from api.services import get_credential
    from api.tasks.azure.peering import list_vnet_peerings_for_cluster

    try:
        return list_vnet_peerings_for_cluster(
            get_credential(),
            subscription_id=sub,
            cluster_resource_group=rg,
            cluster_name=cluster,
        )
    except Exception as exc:
        LOGGER.exception("settings/vnet-peering/existing helper failed")
        raise HTTPException(
            status_code=502,
            detail={
                "code": "vnet_peering_unavailable",
                "message": (
                    "Existing VNet peerings could not be listed: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                ),
            },
        ) from exc


@router.post("/vnet-peering")
def peer_vnet(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    subscription_id = _require(body.get("subscription_id"), _RE_SUB, "subscription_id")
    resource_group = _require(body.get("resource_group"), _RE_RG, "resource_group")
    cluster_name = _require(body.get("cluster_name"), _RE_NAME, "cluster_name")
    target_subscription_id = _require(
        body.get("target_subscription_id"), _RE_SUB, "target_subscription_id"
    )
    target_resource_group = _require(
        body.get("target_resource_group"), _RE_RG, "target_resource_group"
    )
    target_vnet_name = _require(
        body.get("target_vnet_name"), _RE_NAME, "target_vnet_name"
    )

    target_ip = str(body.get("target_ip") or "10.224.0.7").strip()
    _validate_target_ip(target_ip, redact_oid(caller.object_id) or "")

    target_path = str(body.get("target_path") or "/openapi.json").strip()
    if not target_path.startswith("/"):
        target_path = f"/{target_path}"
    if len(target_path) > 256:
        LOGGER.warning(
            "settings/vnet-peering rejected oversize target_path len=%s caller_oid=%s",
            len(target_path),
            redact_oid(caller.object_id),
        )
        raise HTTPException(400, "target_path too long (max 256 chars)")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target_path):
        LOGGER.warning(
            "settings/vnet-peering rejected target_path with control chars caller_oid=%s",
            redact_oid(caller.object_id),
        )
        raise HTTPException(400, "target_path contains control characters")

    LOGGER.info(
        "settings/vnet-peering requested cluster=%s rg=%s target=%s/%s caller_oid=%s",
        cluster_name,
        resource_group,
        target_resource_group,
        target_vnet_name,
        redact_oid(caller.object_id),
    )

    from api.services import get_credential
    from api.tasks.azure.peering import ensure_vnet_peering_with_target

    try:
        summary = ensure_vnet_peering_with_target(
            get_credential(),
            subscription_id=subscription_id,
            cluster_resource_group=resource_group,
            cluster_name=cluster_name,
            target_subscription_id=target_subscription_id,
            target_resource_group=target_resource_group,
            target_vnet_name=target_vnet_name,
            target_ip=target_ip,
            target_path=target_path,
        )
    except Exception as exc:
        LOGGER.exception("settings/vnet-peering helper failed")
        raise HTTPException(
            status_code=502,
            detail={
                "code": "vnet_peering_unavailable",
                "message": (
                    "VNet peering could not be evaluated: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                ),
            },
        ) from exc

    return summary


def _validate_target_ip(target_ip: str, caller_oid: str) -> ipaddress.IPv4Address:
    """Shared IPv4 / RFC1918 validation for both peering endpoints."""

    try:
        addr = ipaddress.IPv4Address(target_ip)
    except (ipaddress.AddressValueError, ValueError) as exc:
        LOGGER.warning(
            "settings/vnet-peering rejected non-IPv4 target_ip=%r caller_oid=%s",
            target_ip,
            caller_oid,
        )
        raise HTTPException(400, "invalid target_ip (IPv4 required)") from exc
    if (
        not addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        LOGGER.warning(
            "settings/vnet-peering rejected non-private target_ip=%s caller_oid=%s",
            target_ip,
            caller_oid,
        )
        raise HTTPException(
            400,
            "target_ip must be an RFC1918 private IPv4 address "
            "(not loopback / link-local / multicast)",
        )
    return addr


@router.post("/vnet-peering/apply-nsg-rule")
def apply_peering_nsg_rule(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Explicit operator action: write an inbound-allow rule on the
    target subnet's NSG so the dashboard probe can reach a private
    workload IP. Only runs when the caller explicitly invokes it from
    the Settings UI; the source CIDR + destination IP + port set are
    all derived from the resolved VNet pair (not from the request body)
    so an authenticated caller cannot turn this into a "punch any NSG"
    primitive.

    Supports ``dry_run=True`` so the SPA can render a 2-step confirm
    (preview the planned rule, then commit) without an extra round-trip
    contract on the wire.
    """
    subscription_id = _require(body.get("subscription_id"), _RE_SUB, "subscription_id")
    resource_group = _require(body.get("resource_group"), _RE_RG, "resource_group")
    cluster_name = _require(body.get("cluster_name"), _RE_NAME, "cluster_name")
    target_subscription_id = _require(
        body.get("target_subscription_id"), _RE_SUB, "target_subscription_id"
    )
    target_resource_group = _require(
        body.get("target_resource_group"), _RE_RG, "target_resource_group"
    )
    target_vnet_name = _require(
        body.get("target_vnet_name"), _RE_NAME, "target_vnet_name"
    )

    target_ip = str(body.get("target_ip") or "10.224.0.7").strip()
    _validate_target_ip(target_ip, redact_oid(caller.object_id) or "")

    dry_run = bool(body.get("dry_run", False))

    # Port allowlist — silently dedupe, sort, refuse anything outside {80, 443}.
    raw_ports = body.get("ports") or [80, 443]
    if not isinstance(raw_ports, list):
        raise HTTPException(400, "ports must be a list of integers")
    try:
        ports_set = {int(p) for p in raw_ports}
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "ports must contain integers only") from exc
    from api.tasks.azure.peering_nsg import (
        ALLOWED_PORTS,
        apply_inbound_allow_rule,
        deterministic_rule_name,
        has_nsg_write_permission,
        next_free_priority_best_effort,
        resolve_nsg_context,
        resolve_vnet_pair_for_cluster,
    )

    bad = ports_set - ALLOWED_PORTS
    if bad:
        LOGGER.warning(
            "settings/vnet-peering/apply-nsg-rule rejected ports=%s caller_oid=%s",
            sorted(bad),
            redact_oid(caller.object_id),
        )
        raise HTTPException(
            400, f"ports must be a subset of {sorted(ALLOWED_PORTS)}"
        )
    if not ports_set:
        raise HTTPException(400, "ports must not be empty")
    ports_sorted = sorted(ports_set)

    # Audit rows are created inside `_audit_session` for the apply path
    # and inside the permission-denied branch as a one-shot record.
    # Validation/discovery failures (lookup_failed, no_nsg_attached,
    # nsg_apply_busy) and dry-run previews intentionally skip audit so
    # the Audit screen never sees phantom "started" rows for actions
    # that did not mutate ARM.

    LOGGER.info(
        "settings/vnet-peering/apply-nsg-rule requested cluster=%s rg=%s "
        "target=%s/%s target_ip=%s ports=%s dry_run=%s caller_oid=%s",
        cluster_name,
        resource_group,
        target_resource_group,
        target_vnet_name,
        target_ip,
        ports_sorted,
        dry_run,
        redact_oid(caller.object_id),
    )

    from api.services import get_credential

    cred = get_credential()
    try:
        aks_vnet_id, target_vnet_id = resolve_vnet_pair_for_cluster(
            cred,
            subscription_id=subscription_id,
            cluster_resource_group=resource_group,
            cluster_name=cluster_name,
            target_subscription_id=target_subscription_id,
            target_resource_group=target_resource_group,
            target_vnet_name=target_vnet_name,
        )
    except LookupError as exc:
        LOGGER.info(
            "settings/vnet-peering/apply-nsg-rule lookup failed: %s caller_oid=%s",
            exc,
            redact_oid(caller.object_id),
        )
        # No ARM mutation happened; skip the audit row to avoid phantom
        # "started" entries the Audit screen has to filter out.
        # Audit P1 #8: sanitise + cap exception text.
        raise HTTPException(404, sanitise(str(exc))[:200]) from exc

    nsg_ctx = resolve_nsg_context(
        cred,
        aks_vnet_id=aks_vnet_id,
        target_vnet_id=target_vnet_id,
        target_ip=target_ip,
    )
    if nsg_ctx is None:
        # Discovery miss — no audit row (the operator never reached the
        # ARM mutation step).
        return {
            "applied": False,
            "skipped_reason": "target_ip_not_in_any_subnet",
            "aks_vnet_id": aks_vnet_id,
            "target_vnet_id": target_vnet_id,
            "target_ip": target_ip,
        }
    if not nsg_ctx.nsg_id:
        return {
            "applied": False,
            "skipped_reason": "no_nsg_attached",
            "nsg_context": nsg_ctx.to_dict(),
        }

    # `nsg_id` is set but the SDK returned a malformed ARM id so the
    # parser couldn't extract sub / rg / name. Refuse explicitly instead
    # of relying on `assert` (stripped under `python -O`).
    if not (nsg_ctx.nsg_subscription_id and nsg_ctx.nsg_resource_group and nsg_ctx.nsg_name):
        LOGGER.error(
            "settings/vnet-peering/apply-nsg-rule nsg_id parsed to empty fields "
            "nsg_id=%s caller_oid=%s",
            nsg_ctx.nsg_id,
            redact_oid(caller.object_id),
        )
        # No mutation attempted; skip audit.
        raise HTTPException(
            status_code=500,
            detail={
                "code": "nsg_id_parse_mismatch",
                "message": "Target subnet's NSG id could not be parsed; aborting.",
            },
        )

    # Dry-run preview must not stall the UI on a flaky ARM. Apply gets
    # the full retry budget; preview gets a single attempt so a 30s
    # `Retry-After` doesn't render an unresponsive form.
    preview_arm_attempts: int | None = 1 if dry_run else None

    if not has_nsg_write_permission(
        cred,
        subscription_id=nsg_ctx.nsg_subscription_id,
        resource_group=nsg_ctx.nsg_resource_group,
        nsg_name=nsg_ctx.nsg_name,
        arm_attempts=preview_arm_attempts,
    ):
        # Best-effort: try to read the NSG's existing rules to compute
        # the actual next-free priority. If the caller also lacks
        # `securityRules/read` (common in tightly-scoped roles) we fall
        # back to ``None`` and render the placeholder, so the printed
        # CLI still works — just with a generic priority comment.
        planned_priority = next_free_priority_best_effort(
            cred,
            nsg_subscription_id=nsg_ctx.nsg_subscription_id,
            nsg_resource_group=nsg_ctx.nsg_resource_group,
            nsg_name=nsg_ctx.nsg_name,
        )
        cli_hint = _nsg_cli_hint(
            nsg_subscription_id=nsg_ctx.nsg_subscription_id,
            nsg_resource_group=nsg_ctx.nsg_resource_group,
            nsg_name=nsg_ctx.nsg_name,
            aks_vnet_id=aks_vnet_id,
            source_prefixes=nsg_ctx.aks_vnet_address_prefixes,
            destination_ip=target_ip,
            ports=ports_sorted,
            planned_priority=planned_priority,
        )
        LOGGER.info(
            "settings/vnet-peering/apply-nsg-rule permission denied nsg=%s caller_oid=%s",
            nsg_ctx.nsg_id,
            redact_oid(caller.object_id),
        )
        # Permission-denied is a legitimate operator action but does not
        # mutate ARM — record it as a one-shot audit row (start + refused
        # in the same call site) so the Audit screen still surfaces the
        # attempt without leaving an orphan "started".
        denied_audit_id = _record_audit_started(
            op="nsg_apply_refused",
            caller=caller,
            target_nsg_name=target_vnet_name,
            destination_ip=target_ip,
            extra={
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
                "nsg_id": nsg_ctx.nsg_id,
                "ports": ports_sorted,
                "dry_run": dry_run,
            },
        )
        _record_audit_event(
            denied_audit_id,
            "refused",
            {"reason": "permission_denied", "nsg_id": nsg_ctx.nsg_id},
        )
        return {
            "applied": False,
            "skipped_reason": "permission_denied",
            "nsg_context": nsg_ctx.to_dict(),
            "cli_hint": cli_hint,
        }

    # Per-NSG serialisation: prevents double-clicks / parallel tabs from
    # racing on the same nsg_id and confusing the priority picker. The
    # lock is Redis-backed when the broker is reachable and falls back
    # to an in-process ``threading.Lock`` with TTL eviction so unit
    # tests work without a live broker.
    from api.services.peering_nsg_lock import (
        NSG_LOCK_DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
        NSG_LOCK_DEFAULT_TTL_SECONDS,
        acquire_nsg_lock,
    )

    handle = acquire_nsg_lock(
        nsg_ctx.nsg_id,
        timeout_seconds=NSG_LOCK_DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
        ttl_seconds=NSG_LOCK_DEFAULT_TTL_SECONDS,
    )
    if handle is None:
        LOGGER.warning(
            "settings/vnet-peering/apply-nsg-rule lock busy nsg=%s caller_oid=%s",
            nsg_ctx.nsg_id,
            redact_oid(caller.object_id),
        )
        # Lock-busy paths do not mutate ARM either; skip audit.
        raise HTTPException(
            status_code=503,
            detail={
                "code": "nsg_apply_busy",
                "message": (
                    "Another operator is currently applying a rule to this NSG. "
                    "Try again in a few seconds."
                ),
            },
        )

    # Dry-run is a preview action with no ARM mutation, so it skips
    # audit entirely (a follow-up real apply creates the single audit
    # row). All other paths go through `_audit_session` which guarantees
    # a terminal event even when the ARM call or `handle.release()`
    # blows up unexpectedly — so the Audit screen never strands a row
    # in `started` state.
    if dry_run:
        try:
            apply_result = apply_inbound_allow_rule(
                cred,
                nsg_subscription_id=nsg_ctx.nsg_subscription_id,
                nsg_resource_group=nsg_ctx.nsg_resource_group,
                nsg_name=nsg_ctx.nsg_name,
                aks_vnet_id=aks_vnet_id,
                source_prefixes=nsg_ctx.aks_vnet_address_prefixes,
                destination_ip=target_ip,
                ports=ports_sorted,
                dry_run=True,
                arm_attempts=preview_arm_attempts,
            )
        except ValueError as exc:
            # Audit P1 #8: sanitise + cap exception text.
            raise HTTPException(400, sanitise(str(exc))[:200]) from exc
        except Exception as exc:
            LOGGER.exception("settings/vnet-peering/apply-nsg-rule dry-run helper failed")
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "nsg_rule_apply_unavailable",
                    "message": (
                        f"NSG rule dry-run could not be computed: "
                        f"{type(exc).__name__}: {str(exc)[:200]}"
                    ),
                },
            ) from exc
        finally:
            handle.release()
    else:
        with _audit_session(
            op="nsg_apply",
            caller=caller,
            target_nsg_name=target_vnet_name,
            destination_ip=target_ip,
            extra={
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
                "target_subscription_id": target_subscription_id,
                "target_resource_group": target_resource_group,
                "target_vnet_name": target_vnet_name,
                "ports": ports_sorted,
            },
        ) as (audit_job_id, set_audit_terminal):
            try:
                try:
                    apply_result = apply_inbound_allow_rule(
                        cred,
                        nsg_subscription_id=nsg_ctx.nsg_subscription_id,
                        nsg_resource_group=nsg_ctx.nsg_resource_group,
                        nsg_name=nsg_ctx.nsg_name,
                        aks_vnet_id=aks_vnet_id,
                        source_prefixes=nsg_ctx.aks_vnet_address_prefixes,
                        destination_ip=target_ip,
                        ports=ports_sorted,
                        dry_run=False,
                        arm_attempts=preview_arm_attempts,
                    )
                except ValueError as exc:
                    LOGGER.warning(
                        "settings/vnet-peering/apply-nsg-rule helper refused: %s caller_oid=%s",
                        exc,
                        redact_oid(caller.object_id),
                    )
                    set_audit_terminal(
                        "refused",
                        {"reason": "helper_validation", "error": str(exc)[:200]},
                    )
                    # Audit P1 #8: sanitise + cap exception text.
                    raise HTTPException(400, sanitise(str(exc))[:200]) from exc
                except Exception as exc:
                    LOGGER.exception("settings/vnet-peering/apply-nsg-rule helper failed")
                    set_audit_terminal(
                        "failed",
                        {
                            "reason": "helper_exception",
                            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                        },
                    )
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "code": "nsg_rule_apply_unavailable",
                            "message": (
                                f"NSG rule could not be applied: "
                                f"{type(exc).__name__}: {str(exc)[:200]}"
                            ),
                        },
                    ) from exc
                set_audit_terminal(
                    "completed",
                    {
                        "applied": apply_result.applied,
                        "skipped_reason": apply_result.skipped_reason,
                        "rule_name": apply_result.rule_name,
                        "priority": apply_result.priority,
                        "nsg_id": apply_result.nsg_id,
                    },
                )
            finally:
                # ``handle.release()`` is best-effort; the Redis backend
                # already swallows EVALSHA errors and the memory backend
                # is locally guaranteed. Any leftover exception here
                # would otherwise overwrite an already-recorded
                # ``completed`` event with no trace. Catch + append a
                # follow-up audit event so operators see "completed but
                # the lock release failed" instead of a silent 500.
                try:
                    handle.release()
                except Exception as release_exc:
                    LOGGER.warning(
                        "settings/vnet-peering/apply-nsg-rule lock release "
                        "failed after completion nsg=%s err=%s",
                        nsg_ctx.nsg_id,
                        type(release_exc).__name__,
                    )
                    _record_audit_event(
                        audit_job_id,
                        "release_error",
                        {
                            "error": (
                                f"{type(release_exc).__name__}: "
                                f"{str(release_exc)[:200]}"
                            ),
                            "nsg_id": nsg_ctx.nsg_id,
                        },
                    )

    LOGGER.info(
        "settings/vnet-peering/apply-nsg-rule completed applied=%s reason=%s rule=%s caller_oid=%s",
        apply_result.applied,
        apply_result.skipped_reason,
        apply_result.rule_name,
        redact_oid(caller.object_id),
    )
    # Echo the deterministic rule name so the SPA can render the preview
    # without having to recompute the hash.
    planned_rule_name = deterministic_rule_name(aks_vnet_id, target_ip)
    return {
        "applied": apply_result.applied,
        "skipped_reason": apply_result.skipped_reason,
        "rule": apply_result.to_dict(),
        "nsg_context": nsg_ctx.to_dict(),
        "planned_rule_name": planned_rule_name,
        "dry_run": dry_run,
    }


def _nsg_cli_hint(
    *,
    nsg_subscription_id: str,
    nsg_resource_group: str,
    nsg_name: str,
    aks_vnet_id: str,
    source_prefixes: list[str],
    destination_ip: str,
    ports: list[int],
    planned_priority: int | None = None,
) -> str:
    """Render the exact ``az network nsg rule create`` command for ops to copy.

    Surfaced when the caller lacks ``securityRules/write`` so a human
    can run it under their own (or a privileged) identity without us
    having to host yet-another permissions wizard. The ``--name`` value
    matches the deterministic name the dashboard would use, so a
    privileged admin running the printed CLI produces the exact rule
    the dashboard would re-detect as ``already_present`` on the next
    button click (no duplicate rules).

    When ``planned_priority`` is provided (we managed to list the NSG's
    existing rules under the caller's identity) we render it directly
    and tighten the header comment. Otherwise we fall back to ``4000``
    with a comment instructing the operator to pick the first free slot
    in 4000-4096 - keeping the printed CLI runnable in both branches.
    """
    from api.tasks.azure.peering_nsg import deterministic_rule_name

    if planned_priority is not None:
        header = (
            f"# Next free priority detected: {planned_priority} "
            f"(range 4000-4096). Adjust if another operator beats you to it."
        )
        priority_value = planned_priority
    else:
        header = (
            "# Could not list existing rules under your identity — verify the\n"
            "# next free priority in 4000-4096 manually before running this\n"
            "# command; the placeholder 4000 below WILL fail if any other\n"
            "# rule already uses it."
        )
        priority_value = 4000
    # Bash safety: if the caller did not supply any AKS VNet address
    # prefixes (the route layer normally populates this list, but a
    # tightly-scoped role may strip it), refuse to render a fully
    # runnable command. We emit a bash variable reference instead so
    # `set -u` fails loudly the moment the operator pastes it without
    # supplying the real CIDRs.
    if source_prefixes:
        sources = " ".join(source_prefixes)
    else:
        header = (
            "# AKS VNet address prefixes were not resolvable under your\n"
            "# identity. Replace ${REPLACE_WITH_AKS_VNET_PREFIXES} below\n"
            "# with the actual CIDRs (e.g. 10.224.0.0/12) before running.\n"
        ) + header
        sources = "${REPLACE_WITH_AKS_VNET_PREFIXES}"
    port_list = " ".join(str(p) for p in ports) if ports else "80 443"
    rule_name = deterministic_rule_name(aks_vnet_id, destination_ip)
    return (
        f"{header}\n"
        f"set -euo pipefail\n"
        f"az network nsg rule create "
        f"--subscription {nsg_subscription_id} "
        f"--resource-group {nsg_resource_group} "
        f"--nsg-name {nsg_name} "
        f"--name {rule_name} "
        f"--priority {priority_value} "
        f"--direction Inbound --access Allow --protocol Tcp "
        f"--source-address-prefixes {sources} "
        f"--destination-address-prefixes {destination_ip}/32 "
        f"--destination-port-ranges {port_list}"
    )
