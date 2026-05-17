"""Unit tests for `k8s_list_events` output hardening."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from api.services import k8s_monitoring as km


def _kubeconfig_bytes() -> bytes:
    ca = base64.b64encode(b"ca-cert").decode("ascii")
    return f"""
apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: {ca}
    server: https://aks.example.test
  name: cluster
users:
- name: user
  user: {{}}
contexts: []
""".encode()


def test_k8s_credential_material_cache_reuses_arm_result(monkeypatch) -> None:
    km.reset_k8s_credential_cache()
    monkeypatch.setenv("K8S_CREDENTIAL_CACHE_TTL_SECONDS", "300")
    calls = {"user": 0}

    class ManagedClusters:
        def list_cluster_user_credentials(self, resource_group: str, cluster_name: str):
            calls["user"] += 1
            assert resource_group == "rg"
            assert cluster_name == "aks"
            return SimpleNamespace(kubeconfigs=[SimpleNamespace(value=_kubeconfig_bytes())])

    monkeypatch.setattr(
        km,
        "aks_client",
        lambda _credential, _subscription_id: SimpleNamespace(managed_clusters=ManagedClusters()),
    )

    first = km._get_k8s_credential_material(MagicMock(), "sub", "rg", "aks", admin=False)
    second = km._get_k8s_credential_material(MagicMock(), "sub", "rg", "aks", admin=False)

    assert first is second
    assert first.server == "https://aks.example.test"
    assert calls == {"user": 1}


def _fake_events_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items}


def _patch_session(items: list[dict[str, Any]]):
    """Patch `_get_k8s_session` to return a fake requests session that
    returns the given items list."""
    response = MagicMock()
    response.json.return_value = _fake_events_payload(items)
    response.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.return_value = response
    session.close = MagicMock()
    return patch.object(km, "_get_k8s_session", return_value=(session, "https://aks"))


def test_k8s_list_events_caps_string_fields_and_clamps_count() -> None:
    huge = "x" * 5000
    items = [
        {
            "metadata": {
                "namespace": "ns",
                "name": huge,
                "creationTimestamp": "2026-05-16T00:00:00Z",
            },
            "involvedObject": {"kind": "Pod", "name": huge},
            "source": {"component": huge, "host": huge},
            "type": "Warning",
            "reason": huge,
            "message": huge,
            "count": 99_999_999,  # ridiculous controller storm
            "lastTimestamp": "2026-05-16T00:00:00Z",
        }
    ]
    with _patch_session(items):
        out = km.k8s_list_events(MagicMock(), "sub", "rg", "cluster")
    assert len(out) == 1
    e = out[0]
    # Caps mirror what the dashboard can render and what K8s actually
    # promises (DNS-1123 ≤ 253 for names, 64 for reasons, ≤ 63 for ns).
    assert len(e["name"]) <= 253
    assert len(e["involved_name"]) <= 253
    assert len(e["namespace"]) <= 63
    assert len(e["reason"]) <= 64
    assert len(e["message"]) <= 1024
    assert len(e["source_component"]) <= 64
    assert len(e["source_host"]) <= 253
    # Count clamped to a sane upper bound so the SPA doesn't render
    # 99,999,999.
    assert e["count"] == 1_000_000


def test_k8s_list_events_coerces_invalid_count_and_type() -> None:
    items = [
        {
            "metadata": {"namespace": "ns", "name": "evt-1"},
            "type": "Bogus",  # not in K8s enum
            "reason": "X",
            "message": "y",
            "count": "not a number",
            "lastTimestamp": "2026-05-16T00:00:00Z",
        }
    ]
    with _patch_session(items):
        out = km.k8s_list_events(MagicMock(), "sub", "rg", "cluster")
    assert out[0]["type"] == "Normal"  # closed enum fallback
    assert out[0]["count"] == 1


def test_k8s_list_events_skips_non_dict_items() -> None:
    items = [None, "not an event", {"reason": "Created", "message": "ok"}]  # type: ignore[list-item]
    with _patch_session(items):  # type: ignore[arg-type]
        out = km.k8s_list_events(MagicMock(), "sub", "rg", "cluster")
    assert len(out) == 1
    assert out[0]["reason"] == "Created"


def test_k8s_list_events_rejects_invalid_namespace() -> None:
    import pytest

    with pytest.raises(ValueError):
        km.k8s_list_events(MagicMock(), "sub", "rg", "cluster", namespace="Invalid_NS!")


def test_k8s_list_events_rejects_oversized_limit() -> None:
    import pytest

    with pytest.raises(ValueError):
        km.k8s_list_events(MagicMock(), "sub", "rg", "cluster", limit=10_000)
    with pytest.raises(ValueError):
        km.k8s_list_events(MagicMock(), "sub", "rg", "cluster", limit=0)
