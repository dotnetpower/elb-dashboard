"""Tests for `api.tasks.openapi.reconcile_public_https`.

Module docstring (natural):
Pin the beat reconciler that rehydrates the in-revision Redis cache from
the durable Storage Table singleton. The original bug we're guarding
against is "Container App revision restart wipes Redis, SPA flips to
'Not exposed' even though the cluster's Certificate is still Ready"
— observed live on 2026-05-28. Multi-cluster enumeration is the
post-critique guard: a second setup against a different cluster must
not silently overwrite the first, and the reconciler must process
every per-cluster entry, not just one.

Responsibility: Verify (a) empty durable list is a no-op, (b) cluster
    reachable + cert Ready re-saves into the cache with refreshed
    `cert_expires_at`, (c) cluster reachable but cert not Ready skips
    the cache write for that cluster so disabled clusters do not get
    accidentally advertised, (d) kubeconfig fetch failure short-circuits
    cleanly for that cluster, (e) two clusters reconcile independently
    in a single tick.
Edit boundaries: Reconciler behavioural pin only. The cache schema is
    tested in `test_openapi_public_https.py`.
Risky contracts: Task must NEVER raise — a beat task that crashes spams
    the worker log every minute.
Validation: `uv run pytest -q api/tests/test_openapi_public_https_reconcile.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.tasks.openapi import reconcile_public_https as reconcile_module


def _ok(stdout: str = "") -> dict[str, Any]:
    return {"exit_code": 0, "stdout": stdout, "stderr": ""}


def _fake_kubectl_factory(ready: bool = True) -> Any:
    def fake_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        if "jsonpath={.status.conditions[?(@.type=='Ready')].status}" in args:
            return _ok("True" if ready else "False")
        return _ok()

    return fake_run


def _entry(cluster_name: str, **overrides: Any) -> dict[str, Any]:
    payload = {
        "base_url": f"https://{cluster_name}.koreacentral.cloudapp.azure.com",
        "metadata": {
            "subscription_id": "sub-1",
            "resource_group": "rg-elb-cluster",
            "cluster_name": cluster_name,
            "ingress_lb_ip": "20.249.192.56",
        },
    }
    payload.update(overrides)
    return payload


def test_reconcile_no_durable_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty list → skip silently (Enable was never clicked anywhere)."""
    monkeypatch.setattr(reconcile_module, "list_openapi_public_base_urls", lambda: [])
    result = reconcile_module.reconcile_openapi_public_https.run()
    assert result["status"] == "skipped"
    assert result["reason"] == "no_durable_state"


def test_reconcile_skips_without_cluster_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry that predates metadata-on-save → skipped per-entry."""
    monkeypatch.setattr(
        reconcile_module,
        "list_openapi_public_base_urls",
        lambda: [{"base_url": "https://x.example.com", "metadata": {}}],
    )
    result = reconcile_module.reconcile_openapi_public_https.run()
    assert result["status"] == "reconciled"
    assert result["clusters_total"] == 1
    assert result["per_cluster"] == [
        {"status": "skipped", "reason": "incomplete_metadata"}
    ]


def test_reconcile_refreshes_cache_on_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cluster reachable + cert Ready → save fresh metadata into Redis."""
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(
        reconcile_module,
        "list_openapi_public_base_urls",
        lambda: [_entry("elb-cluster-01")],
    )
    monkeypatch.setattr(
        reconcile_module,
        "ensure_admin_kubeconfig",
        lambda **_kw: "/tmp/exec/kubeconfig-fake",  # noqa: S108 - test stub
    )
    monkeypatch.setattr(reconcile_module, "kubectl_run", _fake_kubectl_factory(ready=True))
    monkeypatch.setattr(
        reconcile_module,
        "read_certificate_expiry",
        lambda *, kubeconfig_path: "2026-11-21T12:15:13Z",
    )
    monkeypatch.setattr(
        reconcile_module,
        "save_openapi_public_base_url",
        lambda url, **kw: saved.append({"url": url, **kw}) or True,
    )
    result = reconcile_module.reconcile_openapi_public_https.run()
    assert result["status"] == "reconciled"
    assert result["clusters_total"] == 1
    assert result["per_cluster"][0]["cluster_name"] == "elb-cluster-01"
    assert result["per_cluster"][0]["cert_expires_at"] == "2026-11-21T12:15:13Z"
    assert len(saved) == 1
    assert saved[0]["metadata"]["source"] == "reconcile_openapi_public_https"
    assert saved[0]["metadata"]["cert_expires_at"] == "2026-11-21T12:15:13Z"
    # cluster_arm_id is required so reconciler writes back into the
    # right per-cluster slot (not the legacy single key).
    assert "cluster_arm_id" in saved[0]
    assert "elb-cluster-01" in saved[0]["cluster_arm_id"]


def test_reconcile_skips_when_cert_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cluster reachable but cert Ready=False (e.g. mid-renewal) → leave cache alone."""
    saved: list[Any] = []
    monkeypatch.setattr(
        reconcile_module,
        "list_openapi_public_base_urls",
        lambda: [_entry("elb-cluster-01")],
    )
    monkeypatch.setattr(
        reconcile_module,
        "ensure_admin_kubeconfig",
        lambda **_kw: "/tmp/exec/kubeconfig-fake",  # noqa: S108 - test stub
    )
    monkeypatch.setattr(reconcile_module, "kubectl_run", _fake_kubectl_factory(ready=False))
    monkeypatch.setattr(
        reconcile_module,
        "save_openapi_public_base_url",
        lambda *a, **kw: saved.append((a, kw)) or True,
    )
    result = reconcile_module.reconcile_openapi_public_https.run()
    assert result["per_cluster"][0]["status"] == "skipped"
    assert result["per_cluster"][0]["reason"] == "cert_not_ready"
    assert saved == []


def test_reconcile_skips_when_kubeconfig_fetch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cluster unreachable (kubeconfig fetch raises) → skip without raising."""
    monkeypatch.setattr(
        reconcile_module,
        "list_openapi_public_base_urls",
        lambda: [_entry("elb-cluster-01")],
    )

    def boom(**_kw: Any) -> str:
        raise RuntimeError("AKS cluster not found")

    monkeypatch.setattr(reconcile_module, "ensure_admin_kubeconfig", boom)
    result = reconcile_module.reconcile_openapi_public_https.run()
    assert result["per_cluster"][0]["status"] == "skipped"
    assert result["per_cluster"][0]["reason"] == "kubeconfig_unavailable"


def test_reconcile_handles_multiple_clusters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two clusters reconcile independently in a single tick — one cert
    Ready, the other unreachable — and per-cluster failures must not
    short-circuit the rest of the list (#2 multi-cluster guard)."""
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(
        reconcile_module,
        "list_openapi_public_base_urls",
        lambda: [_entry("cluster-a"), _entry("cluster-b")],
    )

    def kubeconfig(*, cluster_name: str, **_kw: Any) -> str:
        if cluster_name == "cluster-b":
            raise RuntimeError("not reachable")
        return "/tmp/exec/kubeconfig-fake"  # noqa: S108 - test stub

    monkeypatch.setattr(reconcile_module, "ensure_admin_kubeconfig", kubeconfig)
    monkeypatch.setattr(reconcile_module, "kubectl_run", _fake_kubectl_factory(ready=True))
    monkeypatch.setattr(
        reconcile_module,
        "read_certificate_expiry",
        lambda *, kubeconfig_path: "2027-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        reconcile_module,
        "save_openapi_public_base_url",
        lambda url, **kw: saved.append({"url": url, **kw}) or True,
    )

    result = reconcile_module.reconcile_openapi_public_https.run()
    assert result["status"] == "reconciled"
    assert result["clusters_total"] == 2
    statuses = {entry["cluster_name"]: entry["status"] for entry in result["per_cluster"]}
    assert statuses == {"cluster-a": "reconciled", "cluster-b": "skipped"}
    # Only the reachable cluster's cache got refreshed.
    assert len(saved) == 1
    assert saved[0]["cluster_arm_id"].endswith("cluster-a")
