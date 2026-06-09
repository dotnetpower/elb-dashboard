"""Tests for the `/api/settings/vnet-peering` settings route.

Responsibility: Cover input validation and the synchronous summary return
shape exposed to the Settings panel.
Edit boundaries: HTTP shaping only. Azure work is stubbed out.
Key entry points: `peer_vnet`.
Risky contracts: The helper returns best-effort payloads with `error` on
partial failures; the route forwards those values instead of bubbling the
Azure exception to the UI.
Validation: `uv run pytest -q api/tests/test_settings_vnet_peering.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.auth import CallerIdentity, require_caller
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_nsg_locks(monkeypatch) -> None:
    """Isolate the per-NSG apply lock for each test, independent of Redis.

    ``acquire_nsg_lock`` prefers a Redis backend when one is reachable and
    only falls back to a process-global in-memory dict. A developer machine
    (or another test session) commonly has a local Redis on 6379 for the
    worker/beat sidecars, so the lock lands in Redis with a 180s TTL and the
    route's best-effort ``handle.release()`` is the only thing that frees it.
    Tests reuse the same ``nsg_id``, so any unreleased / TTL-pinned key makes
    the next apply return ``nsg_apply_busy`` (503) — green in CI (no Redis)
    but red locally. Forcing the in-memory backend and clearing it before
    each test makes the suite deterministic in both environments.
    """
    from api.services import peering_nsg_lock

    monkeypatch.setattr(peering_nsg_lock, "_redis_client_or_none", lambda: None)
    peering_nsg_lock.reset_memory_locks_for_tests()


def _build_app(monkeypatch):
    from api.routes.settings import vnet_peering as settings_route
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(settings_route.router)
    app.dependency_overrides[require_caller] = lambda: CallerIdentity(
        object_id="caller-1",
        tenant_id="tenant-1",
        upn="alice@example.com",
        raw_token="",
        claims={},
    )
    return app


def test_route_rejects_missing_parameters(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    resp = client.post("/vnet-peering", json={"subscription_id": "x"})

    assert resp.status_code == 400


def test_route_returns_helper_summary(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    def _fake_ensure(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "target_vnet": (
                "/subscriptions/sub-2/resourceGroups/rg-target/"
                "providers/Microsoft.Network/virtualNetworks/vnet-target"
            ),
            "peerings": [
                {
                    "direction": "target_to_aks",
                    "name": "peer-target-to-aks",
                    "state": "Connected",
                },
                {
                    "direction": "aks_to_target",
                    "name": "peer-aks-to-target",
                    "state": "Connected",
                },
            ],
            "probe": {
                "target_ip": "10.224.0.7",
                "reachable": True,
                "status_code": 200,
                "latency_ms": 10.0,
                "message": "OK",
            },
        }

    monkeypatch.setattr("api.tasks.azure.peering.ensure_vnet_peering_with_target", _fake_ensure)
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    resp = client.post(
        "/vnet-peering",
        json={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-workload",
            "cluster_name": "elb-cluster-01",
            "target_subscription_id": "00000000-0000-0000-0000-000000000002",
            "target_resource_group": "rg-target",
            "target_vnet_name": "vnet-target",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["probe"]["reachable"] is True
    assert {p["direction"] for p in resp.json()["peerings"]} == {
        "target_to_aks",
        "aks_to_target",
    }


def test_route_returns_502_when_helper_raises(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ARM down")

    monkeypatch.setattr("api.tasks.azure.peering.ensure_vnet_peering_with_target", _boom)
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    resp = client.post(
        "/vnet-peering",
        json={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-workload",
            "cluster_name": "elb-cluster-01",
            "target_subscription_id": "00000000-0000-0000-0000-000000000002",
            "target_resource_group": "rg-target",
            "target_vnet_name": "vnet-target",
        },
    )

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "vnet_peering_unavailable"


def test_route_rejects_non_private_target_ip(monkeypatch) -> None:
    """SSRF guard: target_ip must be RFC1918 private space."""

    app = _build_app(monkeypatch)
    client = TestClient(app)

    # Tracks whether the helper would have been called — it must NOT be,
    # since the rejection happens before we hand off to ARM/probe.
    called = {"n": 0}

    def _spy(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        called["n"] += 1
        return {}

    monkeypatch.setattr("api.tasks.azure.peering.ensure_vnet_peering_with_target", _spy)
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    base = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "resource_group": "rg-workload",
        "cluster_name": "elb-cluster-01",
        "target_subscription_id": "00000000-0000-0000-0000-000000000002",
        "target_resource_group": "rg-target",
        "target_vnet_name": "vnet-target",
    }

    for hostile_ip in (
        "169.254.169.254",  # Azure IMDS
        "127.0.0.1",  # loopback
        "8.8.8.8",  # public
        "224.0.0.1",  # multicast
        "0.0.0.0",  # unspecified
        "fd00::1",  # IPv6 ULA — IPv4 required
        "::ffff:169.254.169.254",  # IPv4-mapped IMDS bypass attempt
        "not-an-ip",
    ):
        resp = client.post("/vnet-peering", json={**base, "target_ip": hostile_ip})
        assert resp.status_code == 400, f"expected 400 for {hostile_ip!r}, got {resp.status_code}"

    assert called["n"] == 0


def test_route_rejects_unsafe_target_path(monkeypatch) -> None:
    """SSRF guard: target_path must be a normal absolute path."""

    app = _build_app(monkeypatch)
    client = TestClient(app)

    monkeypatch.setattr(
        "api.tasks.azure.peering.ensure_vnet_peering_with_target",
        lambda *_a, **_k: {},
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    base = {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "resource_group": "rg-workload",
        "cluster_name": "elb-cluster-01",
        "target_subscription_id": "00000000-0000-0000-0000-000000000002",
        "target_resource_group": "rg-target",
        "target_vnet_name": "vnet-target",
        "target_ip": "10.224.0.7",
    }

    # CRLF injection.
    resp = client.post(
        "/vnet-peering",
        json={**base, "target_path": "/openapi.json\r\nHost: evil"},
    )
    assert resp.status_code == 400

    # Over-long path.
    resp = client.post("/vnet-peering", json={**base, "target_path": "/" + "a" * 300})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /vnet-peering/apply-nsg-rule
# ---------------------------------------------------------------------------


def _nsg_base() -> dict[str, Any]:
    return {
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "resource_group": "rg-workload",
        "cluster_name": "elb-cluster-01",
        "target_subscription_id": "00000000-0000-0000-0000-000000000002",
        "target_resource_group": "rg-target",
        "target_vnet_name": "vnet-target",
        "target_ip": "10.0.1.50",
    }


def _stub_credential_and_resolve(
    monkeypatch,
    *,
    aks_vnet_id: str = (
        "/subscriptions/sub-aks/resourceGroups/MC_rg-aks_clu_kr/"
        "providers/Microsoft.Network/virtualNetworks/aks-vnet-1"
    ),
    target_vnet_id: str = (
        "/subscriptions/sub-target/resourceGroups/rg-target/"
        "providers/Microsoft.Network/virtualNetworks/vnet-target"
    ),
) -> None:
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        "api.tasks.azure.peering_nsg.resolve_vnet_pair_for_cluster",
        lambda *_a, **_k: (aks_vnet_id, target_vnet_id),
    )


def test_apply_nsg_rejects_non_private_target_ip(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    resp = client.post(
        "/vnet-peering/apply-nsg-rule",
        json={**_nsg_base(), "target_ip": "169.254.169.254"},
    )
    assert resp.status_code == 400


def test_apply_nsg_rejects_disallowed_port(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    resp = client.post(
        "/vnet-peering/apply-nsg-rule",
        json={**_nsg_base(), "ports": [22, 443]},
    )
    assert resp.status_code == 400


def test_apply_nsg_returns_no_nsg_attached(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)
    _stub_credential_and_resolve(monkeypatch)

    from api.tasks.azure.peering_nsg import NsgContext

    def _fake_resolve(*_a: Any, **_k: Any) -> NsgContext:
        return NsgContext(
            target_subnet_id=(
                "/subscriptions/sub-target/resourceGroups/rg-target/"
                "providers/Microsoft.Network/virtualNetworks/vnet-target/subnets/backend"
            ),
            target_subnet_name="backend",
            target_subnet_prefixes=["10.0.2.0/24"],
            nsg_id=None,
            nsg_subscription_id=None,
            nsg_resource_group=None,
            nsg_name=None,
            aks_vnet_address_prefixes=["10.224.0.0/12"],
            target_ip="10.0.2.99",
        )

    monkeypatch.setattr("api.tasks.azure.peering_nsg.resolve_nsg_context", _fake_resolve)

    resp = client.post(
        "/vnet-peering/apply-nsg-rule",
        json={**_nsg_base(), "target_ip": "10.0.2.99"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    assert body["skipped_reason"] == "no_nsg_attached"


def test_apply_nsg_returns_target_ip_not_in_any_subnet(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)
    _stub_credential_and_resolve(monkeypatch)

    monkeypatch.setattr(
        "api.tasks.azure.peering_nsg.resolve_nsg_context",
        lambda *_a, **_k: None,
    )

    resp = client.post(
        "/vnet-peering/apply-nsg-rule",
        json={**_nsg_base(), "target_ip": "10.99.99.99"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    assert body["skipped_reason"] == "target_ip_not_in_any_subnet"


def test_apply_nsg_writes_rule_when_permitted(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)
    _stub_credential_and_resolve(monkeypatch)

    from api.tasks.azure.peering_nsg import ApplyResult, NsgContext

    monkeypatch.setattr(
        "api.tasks.azure.peering_nsg.resolve_nsg_context",
        lambda *_a, **_k: NsgContext(
            target_subnet_id="subnet-id",
            target_subnet_name="frontend",
            target_subnet_prefixes=["10.0.1.0/24"],
            nsg_id=(
                "/subscriptions/sub-target/resourceGroups/rg-target/"
                "providers/Microsoft.Network/networkSecurityGroups/nsg-frontend"
            ),
            nsg_subscription_id="sub-target",
            nsg_resource_group="rg-target",
            nsg_name="nsg-frontend",
            aks_vnet_address_prefixes=["10.224.0.0/12"],
            target_ip="10.0.1.50",
        ),
    )
    monkeypatch.setattr(
        "api.tasks.azure.peering_nsg.has_nsg_write_permission",
        lambda *_a, **_k: True,
    )

    captured: dict[str, Any] = {}

    def _fake_apply(*_a: Any, **kwargs: Any) -> ApplyResult:
        captured.update(kwargs)
        return ApplyResult(
            applied=True,
            rule_name="elb-dashboard-allow-aks-deadbeef",
            nsg_id=kwargs["nsg_subscription_id"],
            priority=4000,
            source_prefixes=kwargs["source_prefixes"],
            destination_ip=kwargs["destination_ip"],
            ports=kwargs["ports"],
        )

    monkeypatch.setattr("api.tasks.azure.peering_nsg.apply_inbound_allow_rule", _fake_apply)

    resp = client.post(
        "/vnet-peering/apply-nsg-rule",
        json=_nsg_base(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["rule"]["rule_name"] == "elb-dashboard-allow-aks-deadbeef"
    # SSRF + scope guard: source must come from the AKS VNet, not from caller input.
    assert captured["source_prefixes"] == ["10.224.0.0/12"]
    assert captured["destination_ip"] == "10.0.1.50"
    assert captured["ports"] == [80, 443]


def test_apply_nsg_returns_404_when_vnet_pair_lookup_fails(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    def _boom(*_a: Any, **_k: Any) -> tuple[str, str]:
        raise LookupError("aks cluster lookup failed: ResourceNotFoundError")

    monkeypatch.setattr("api.tasks.azure.peering_nsg.resolve_vnet_pair_for_cluster", _boom)

    resp = client.post(
        "/vnet-peering/apply-nsg-rule",
        json=_nsg_base(),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Hardening: deterministic CLI hint (#5), 500 on malformed nsg_id (#2),
# audit append-blob recording (#4), dry_run preview (#7), per-NSG
# serialisation lock (#8).
# ---------------------------------------------------------------------------


def _nsg_ctx_with_permission(monkeypatch, *, target_ip: str = "10.0.1.50") -> None:
    """Stand up a valid NsgContext + permission grant + apply stub so the
    later assertions can focus on the diff under test."""
    from api.tasks.azure.peering_nsg import ApplyResult, NsgContext

    monkeypatch.setattr(
        "api.tasks.azure.peering_nsg.resolve_nsg_context",
        lambda *_a, **_k: NsgContext(
            target_subnet_id="subnet-id",
            target_subnet_name="frontend",
            target_subnet_prefixes=["10.0.1.0/24"],
            nsg_id=(
                "/subscriptions/sub-target/resourceGroups/rg-target/"
                "providers/Microsoft.Network/networkSecurityGroups/nsg-frontend"
            ),
            nsg_subscription_id="sub-target",
            nsg_resource_group="rg-target",
            nsg_name="nsg-frontend",
            aks_vnet_address_prefixes=["10.224.0.0/12"],
            target_ip=target_ip,
        ),
    )
    monkeypatch.setattr(
        "api.tasks.azure.peering_nsg.has_nsg_write_permission",
        lambda *_a, **_k: True,
    )

    def _fake_apply(*_a: Any, **kwargs: Any) -> ApplyResult:
        return ApplyResult(
            applied=not kwargs.get("dry_run", False),
            rule_name="elb-dashboard-allow-aks-deadbeef",
            nsg_id="nsg-id",
            priority=4000,
            source_prefixes=kwargs["source_prefixes"],
            destination_ip=kwargs["destination_ip"],
            ports=kwargs["ports"],
            skipped_reason="dry_run" if kwargs.get("dry_run") else None,
        )

    monkeypatch.setattr("api.tasks.azure.peering_nsg.apply_inbound_allow_rule", _fake_apply)


def test_apply_nsg_returns_500_when_nsg_id_parse_fails(monkeypatch) -> None:
    """#2 — replacing the bare ``assert`` with an explicit 500 keeps the
    SPA in a recoverable error state under ``python -O`` (where asserts
    are stripped)."""
    app = _build_app(monkeypatch)
    client = TestClient(app)
    _stub_credential_and_resolve(monkeypatch)

    from api.tasks.azure.peering_nsg import NsgContext

    monkeypatch.setattr(
        "api.tasks.azure.peering_nsg.resolve_nsg_context",
        lambda *_a, **_k: NsgContext(
            target_subnet_id="subnet-id",
            target_subnet_name="frontend",
            target_subnet_prefixes=["10.0.1.0/24"],
            # malformed: nsg_id present but the sub/rg/name parser failed
            nsg_id="malformed-arm-id",
            nsg_subscription_id=None,
            nsg_resource_group=None,
            nsg_name=None,
            aks_vnet_address_prefixes=["10.224.0.0/12"],
            target_ip="10.0.1.50",
        ),
    )

    resp = client.post(
        "/vnet-peering/apply-nsg-rule",
        json=_nsg_base(),
    )
    assert resp.status_code == 500
    assert resp.json()["detail"]["code"] == "nsg_id_parse_mismatch"


def test_apply_nsg_cli_hint_uses_deterministic_name_and_priority_comment(
    monkeypatch,
) -> None:
    """#5 — the CLI hint must print the same deterministic rule name the
    dashboard would write, and warn that 4000 may already be taken."""
    app = _build_app(monkeypatch)
    client = TestClient(app)
    _stub_credential_and_resolve(monkeypatch)

    from api.tasks.azure import peering_nsg
    from api.tasks.azure.peering_nsg import NsgContext

    monkeypatch.setattr(
        "api.tasks.azure.peering_nsg.resolve_nsg_context",
        lambda *_a, **_k: NsgContext(
            target_subnet_id="subnet-id",
            target_subnet_name="frontend",
            target_subnet_prefixes=["10.0.1.0/24"],
            nsg_id=(
                "/subscriptions/sub-target/resourceGroups/rg-target/"
                "providers/Microsoft.Network/networkSecurityGroups/nsg-frontend"
            ),
            nsg_subscription_id="sub-target",
            nsg_resource_group="rg-target",
            nsg_name="nsg-frontend",
            aks_vnet_address_prefixes=["10.224.0.0/12"],
            target_ip="10.0.1.50",
        ),
    )
    monkeypatch.setattr(
        "api.tasks.azure.peering_nsg.has_nsg_write_permission",
        lambda *_a, **_k: False,
    )
    applied: list[Any] = []
    monkeypatch.setattr(
        "api.tasks.azure.peering_nsg.apply_inbound_allow_rule",
        lambda *a, **k: applied.append((a, k)),
    )

    resp = client.post(
        "/vnet-peering/apply-nsg-rule",
        json=_nsg_base(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is False
    assert body["skipped_reason"] == "permission_denied"
    cli_hint = body["cli_hint"]
    expected_name = peering_nsg.deterministic_rule_name(
        "/subscriptions/sub-aks/resourceGroups/MC_rg-aks_clu_kr/"
        "providers/Microsoft.Network/virtualNetworks/aks-vnet-1",
        "10.0.1.50",
    )
    assert "az network nsg rule create" in cli_hint
    assert f"--name {expected_name}" in cli_hint
    assert "10.224.0.0/12" in cli_hint
    assert "10.0.1.50" in cli_hint
    assert "4000-4096" in cli_hint  # priority guidance comment
    assert applied == []


def test_apply_nsg_records_audit_started_and_completed(monkeypatch) -> None:
    """#4 — append-blob audit must mark start + terminal state so the
    Audit screen surfaces the operator action."""
    app = _build_app(monkeypatch)
    client = TestClient(app)
    _stub_credential_and_resolve(monkeypatch)
    _nsg_ctx_with_permission(monkeypatch)

    started: list[dict[str, Any]] = []
    events: list[tuple[str, str, dict[str, Any]]] = []

    def _fake_record(**kwargs: Any) -> str:
        started.append(kwargs)
        return "dbops:nsg_apply:peering:vnet-target:abc123"

    def _fake_event(job_id: str, event: str, payload: dict[str, Any]) -> None:
        events.append((job_id, event, payload))

    monkeypatch.setattr("api.services.db.ops_audit.record_db_op", _fake_record)
    monkeypatch.setattr("api.services.db.ops_audit.record_db_op_event", _fake_event)

    resp = client.post(
        "/vnet-peering/apply-nsg-rule",
        json=_nsg_base(),
    )
    assert resp.status_code == 200, resp.text
    assert len(started) == 1
    assert started[0]["op"] == "nsg_apply"
    assert started[0]["db_name"] == "vnet-target"
    # Exactly one terminal event must follow start.
    assert any(ev == "completed" for _, ev, _ in events)


def test_apply_nsg_dry_run_returns_planned_rule(monkeypatch) -> None:
    """#7 — preview must return ``would_apply``-style payload without an
    ARM write so the SPA can show the planned rule before commit."""
    app = _build_app(monkeypatch)
    client = TestClient(app)
    _stub_credential_and_resolve(monkeypatch)
    _nsg_ctx_with_permission(monkeypatch)

    resp = client.post(
        "/vnet-peering/apply-nsg-rule",
        json={**_nsg_base(), "dry_run": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["skipped_reason"] == "dry_run"
    assert body["dry_run"] is True
    assert body["planned_rule_name"]
    # The route still echoes the helper's ApplyResult under `rule` so the
    # SPA can render the planned priority + scope without a second
    # round-trip.
    assert body["rule"]["priority"] == 4000
    assert body["rule"]["destination_ip"] == "10.0.1.50"


def test_apply_nsg_uses_per_nsg_lock(monkeypatch) -> None:
    """#8 — the route must acquire and release the per-NSG lock around
    the ARM write. Lock-level mutual exclusion is covered in
    ``test_peering_nsg_lock.py``; this test keeps the route contract
    lightweight and deterministic.
    """
    from api.services import peering_nsg_lock as lock_mod

    app = _build_app(monkeypatch)
    client = TestClient(app)
    _stub_credential_and_resolve(monkeypatch)
    _nsg_ctx_with_permission(monkeypatch)

    events: list[tuple[str, str]] = []

    class _Handle:
        def __init__(self, nsg_id: str) -> None:
            self.nsg_id = nsg_id

        def release(self) -> None:
            events.append(("release", self.nsg_id))

    def _fake_acquire(nsg_id: str, **_kwargs: Any) -> _Handle:
        events.append(("acquire", nsg_id))
        return _Handle(nsg_id)

    monkeypatch.setattr(lock_mod, "acquire_nsg_lock", _fake_acquire)

    resp = client.post("/vnet-peering/apply-nsg-rule", json=_nsg_base())
    assert resp.status_code == 200, resp.text
    assert events == [
        (
            "acquire",
            "/subscriptions/sub-target/resourceGroups/rg-target/"
            "providers/Microsoft.Network/networkSecurityGroups/nsg-frontend",
        ),
        (
            "release",
            "/subscriptions/sub-target/resourceGroups/rg-target/"
            "providers/Microsoft.Network/networkSecurityGroups/nsg-frontend",
        ),
    ]


# ---------------------------------------------------------------------------
# GET /vnet-peering/existing
# ---------------------------------------------------------------------------


def test_existing_route_rejects_bad_parameters(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    resp = client.get(
        "/vnet-peering/existing",
        params={
            "subscription_id": "not-a-guid",
            "resource_group": "rg",
            "cluster_name": "elb-cluster-01",
        },
    )
    assert resp.status_code == 400


def test_existing_route_returns_helper_summary(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    captured: dict[str, Any] = {}

    def _fake_list(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "aks_vnet": (
                "/subscriptions/sub-1/resourceGroups/MC_rg/"
                "providers/Microsoft.Network/virtualNetworks/aks-vnet"
            ),
            "aks_vnet_name": "aks-vnet",
            "node_resource_group": "MC_rg",
            "peerings": [
                {
                    "name": "peer-aks-vnet-to-vnet-target",
                    "peering_state": "Connected",
                    "provisioning_state": "Succeeded",
                    "remote_vnet": {
                        "id": (
                            "/subscriptions/sub-2/resourceGroups/rg-target/"
                            "providers/Microsoft.Network/virtualNetworks/vnet-target"
                        ),
                        "name": "vnet-target",
                        "resource_group": "rg-target",
                        "subscription_id": "sub-2",
                    },
                    "remote_address_prefixes": ["10.10.0.0/16"],
                    "allow_virtual_network_access": True,
                    "allow_forwarded_traffic": False,
                    "allow_gateway_transit": False,
                    "use_remote_gateways": False,
                }
            ],
            "skipped": False,
            "reason": None,
            "error": None,
        }

    monkeypatch.setattr(
        "api.tasks.azure.peering.list_vnet_peerings_for_cluster", _fake_list
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    resp = client.get(
        "/vnet-peering/existing",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-workload",
            "cluster_name": "elb-cluster-01",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["aks_vnet_name"] == "aks-vnet"
    assert body["peerings"][0]["peering_state"] == "Connected"
    assert body["peerings"][0]["remote_vnet"]["name"] == "vnet-target"
    # Route forwards validated kwargs to the helper verbatim.
    assert captured["cluster_resource_group"] == "rg-workload"
    assert captured["cluster_name"] == "elb-cluster-01"


def test_existing_route_forwards_degraded_payload(monkeypatch) -> None:
    """A helper-side RBAC denial is folded into a 200 payload (error set)."""

    app = _build_app(monkeypatch)
    client = TestClient(app)

    def _fake_list(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "aks_vnet": "",
            "aks_vnet_name": "",
            "node_resource_group": "MC_rg",
            "peerings": [],
            "skipped": False,
            "reason": None,
            "error": "virtual_network_peerings.list failed: HttpResponseError",
        }

    monkeypatch.setattr(
        "api.tasks.azure.peering.list_vnet_peerings_for_cluster", _fake_list
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    resp = client.get(
        "/vnet-peering/existing",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-workload",
            "cluster_name": "elb-cluster-01",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["error"].startswith("virtual_network_peerings.list failed")


def test_existing_route_returns_502_when_helper_raises(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ARM down")

    monkeypatch.setattr(
        "api.tasks.azure.peering.list_vnet_peerings_for_cluster", _boom
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    resp = client.get(
        "/vnet-peering/existing",
        params={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-workload",
            "cluster_name": "elb-cluster-01",
        },
    )

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "vnet_peering_unavailable"


# ---------------------------------------------------------------------------
# POST /vnet-peering/delete
# ---------------------------------------------------------------------------


def test_delete_route_rejects_bad_parameters(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    resp = client.post(
        "/vnet-peering/delete",
        json={
            "subscription_id": "not-a-guid",
            "resource_group": "rg",
            "cluster_name": "elb-cluster-01",
            "peering_name": "peer-1",
        },
    )
    assert resp.status_code == 400


def test_delete_route_forwards_to_helper(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    captured: dict[str, Any] = {}

    def _fake_delete(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "deleted": True,
            "skipped": False,
            "reason": None,
            "error": None,
            "peering_name": kwargs["peering_name"],
        }

    monkeypatch.setattr(
        "api.tasks.azure.peering.delete_vnet_peering_on_cluster", _fake_delete
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    resp = client.post(
        "/vnet-peering/delete",
        json={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-workload",
            "cluster_name": "elb-cluster-01",
            "peering_name": "peer-aks-vnet-to-vnet-target",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    assert body["peering_name"] == "peer-aks-vnet-to-vnet-target"
    assert captured["cluster_resource_group"] == "rg-workload"
    assert captured["peering_name"] == "peer-aks-vnet-to-vnet-target"


def test_delete_route_returns_502_on_helper_error(monkeypatch) -> None:
    app = _build_app(monkeypatch)
    client = TestClient(app)

    def _fake_delete(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "deleted": False,
            "skipped": False,
            "reason": None,
            "error": "virtual_network_peerings.begin_delete failed: HttpResponseError",
            "peering_name": kwargs["peering_name"],
        }

    monkeypatch.setattr(
        "api.tasks.azure.peering.delete_vnet_peering_on_cluster", _fake_delete
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    resp = client.post(
        "/vnet-peering/delete",
        json={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "resource_group": "rg-workload",
            "cluster_name": "elb-cluster-01",
            "peering_name": "peer-1",
        },
    )

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "vnet_peering_delete_failed"
