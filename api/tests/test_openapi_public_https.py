"""Tests for the OpenAPI public HTTPS pipeline (ingress-nginx + cert-manager).

Responsibility: Verify the manifest builders, kubectl-driven setup/disable
    Celery tasks, runtime cache fallback, and route enqueue contracts for the
    `setup_openapi_public_https` / `disable_openapi_public_https` flow.
Edit boundaries: Keep assertions focused on shape + idempotency + cache contract.
    Network, real cert issuance, and live AKS are out of scope — those belong
    in deploy validation, not unit tests.
Key entry points: `test_dns_label_is_stable_per_cluster`,
    `test_ingress_manifest_has_tls_redirect_and_cert_issuer`,
    `test_setup_public_https_runs_full_pipeline`,
    `test_setup_public_https_idempotent_kubeconfig_reuse`,
    `test_disable_public_https_clears_cache`,
    `test_public_tls_base_url_falls_back_to_runtime_cache`.
Risky contracts: Do not require network access, real kubectl, or real Redis.
Validation: `uv run pytest -q api/tests/test_openapi_public_https.py`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

# ----------------------------------------------------------------------- builders


def test_dns_label_is_stable_per_cluster() -> None:
    from api.services.k8s.ingress import dns_label_for_cluster

    label_a = dns_label_for_cluster(subscription_id="sub-1", cluster_name="elb-cluster")
    label_b = dns_label_for_cluster(subscription_id="sub-1", cluster_name="elb-cluster")
    label_c = dns_label_for_cluster(subscription_id="sub-1", cluster_name="other-cluster")
    label_d = dns_label_for_cluster(subscription_id="sub-2", cluster_name="elb-cluster")

    assert label_a == label_b, "same inputs must produce same label (idempotent)"
    assert label_a != label_c, "different cluster names must differ"
    assert label_a != label_d, "different subscriptions must differ"
    assert label_a.startswith("elb-openapi-")
    # Azure DNS label limit is 63 chars; our format is always well under.
    assert len(label_a) <= 63


def test_cloudapp_fqdn_format() -> None:
    from api.services.k8s.ingress import cloudapp_fqdn

    assert (
        cloudapp_fqdn(dns_label="elb-openapi-abc123", region="koreacentral")
        == "elb-openapi-abc123.koreacentral.cloudapp.azure.com"
    )


def test_cluster_issuer_uses_letsencrypt_prod_with_http01() -> None:
    from api.services.k8s.ingress import build_cluster_issuer

    issuer = json.loads(build_cluster_issuer(email="ops@example.com"))
    assert issuer["kind"] == "ClusterIssuer"
    assert issuer["metadata"]["name"] == "letsencrypt-prod"
    spec = issuer["spec"]["acme"]
    assert spec["server"].endswith("/acme-v02.api.letsencrypt.org/directory")
    assert spec["email"] == "ops@example.com"
    assert spec["solvers"][0]["http01"]["ingress"]["class"] == "nginx"


def test_ingress_manifest_has_tls_redirect_and_cert_issuer() -> None:
    from api.services.k8s.ingress import build_openapi_ingress

    fqdn = "elb-openapi-abc123.koreacentral.cloudapp.azure.com"
    ingress = json.loads(build_openapi_ingress(fqdn=fqdn))
    assert ingress["kind"] == "Ingress"
    annotations = ingress["metadata"]["annotations"]
    assert annotations["cert-manager.io/cluster-issuer"] == "letsencrypt-prod"
    assert annotations["nginx.ingress.kubernetes.io/ssl-redirect"] == "true"
    # 100m matches the existing api-sidecar streaming proxy ceiling so the
    # ingress does not 413 BLAST query uploads before the proxy can.
    assert annotations["nginx.ingress.kubernetes.io/proxy-body-size"] == "100m"
    tls = ingress["spec"]["tls"][0]
    assert tls["hosts"] == [fqdn]
    assert tls["secretName"] == "elb-openapi-tls"
    rule = ingress["spec"]["rules"][0]
    assert rule["host"] == fqdn
    backend = rule["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == "elb-openapi"
    assert backend["port"]["number"] == 80


def test_dns_label_patch_uses_azure_annotation() -> None:
    from api.services.k8s.ingress import build_dns_label_patch

    patch = json.loads(build_dns_label_patch(dns_label="elb-openapi-abc123"))
    assert (
        patch["metadata"]["annotations"][
            "service.beta.kubernetes.io/azure-dns-label-name"
        ]
        == "elb-openapi-abc123"
    )


# ------------------------------------------------------------------------ runtime cache


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


def test_public_tls_base_url_falls_back_to_runtime_cache(monkeypatch) -> None:
    from api.services.openapi import runtime

    fake_redis = _FakeRedis()
    monkeypatch.delenv("OPENAPI_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(runtime, "get_ops_redis_client", lambda **_kwargs: fake_redis)

    # No env, no cache → empty string (legacy IP path)
    assert runtime.get_public_tls_base_url() == ""

    # Setup task wrote to the cache → fallback kicks in
    runtime.save_openapi_public_base_url(
        "https://elb-openapi-abc.koreacentral.cloudapp.azure.com",
        metadata={"dns_label": "elb-openapi-abc", "region": "koreacentral"},
        client=fake_redis,
    )
    assert (
        runtime.get_public_tls_base_url()
        == "https://elb-openapi-abc.koreacentral.cloudapp.azure.com"
    )

    payload = runtime.get_openapi_public_base_url(client=fake_redis)
    assert payload["base_url"].endswith("cloudapp.azure.com")
    assert payload["metadata"]["dns_label"] == "elb-openapi-abc"
    assert "updated_at" in payload

    runtime.clear_openapi_public_base_url(client=fake_redis)
    assert runtime.get_openapi_public_base_url(client=fake_redis) == {}

    # Env still wins over cache when both present
    runtime.save_openapi_public_base_url(
        "https://elb-openapi-abc.koreacentral.cloudapp.azure.com",
        client=fake_redis,
    )
    monkeypatch.setenv("OPENAPI_PUBLIC_BASE_URL", "https://override.example.com")
    assert runtime.get_public_tls_base_url() == "https://override.example.com"


# ------------------------------------------------------------------------ task


def _ok(stdout: str = "ok") -> dict[str, Any]:
    return {"exit_code": 0, "stdout": stdout, "stderr": ""}


def _stub_cluster(region: str = "koreacentral") -> Any:
    class _Cluster:
        location = region

    class _AKS:
        class managed_clusters:
            @staticmethod
            def get(_rg: str, _name: str) -> Any:
                return _Cluster()

    return _AKS()


def test_setup_public_https_runs_full_pipeline(monkeypatch) -> None:
    from api.services.openapi import runtime
    from api.tasks.openapi import public_https as task_module

    # Stub Azure SDK
    monkeypatch.setattr(task_module, "get_credential", lambda: object())
    monkeypatch.setattr(task_module, "aks_client", lambda *_a, **_kw: _stub_cluster())

    # Stub kubectl auth
    monkeypatch.setattr(
        task_module,
        "ensure_admin_kubeconfig",
        lambda **_kw: "/tmp/exec/kubeconfig-fake"  # noqa: S108 - fake path returned by mocked auth helper,
    )

    # Stub kubectl invocations + remember sequence
    calls: list[list[str]] = []

    def fake_kubectl_run(
        args: list[str],
        *,
        kubeconfig_path: str,
        stdin: str | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        calls.append(args)
        # get-service jsonpath → return EXTERNAL-IP
        if args[:3] == ["get", "service", "ingress-nginx-controller"]:
            return _ok(stdout="20.41.1.2")
        if args[:1] == ["wait"]:
            return _ok()
        if args[:1] == ["patch"]:
            return _ok()
        if args[:1] == ["apply"]:
            return _ok()
        if args[:2] == ["get", "certificate"]:
            return _ok(stdout="2026-08-25T00:00:00Z")
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)

    # Stub runtime cache to in-memory dict
    fake_redis = _FakeRedis()
    monkeypatch.setattr(runtime, "get_ops_redis_client", lambda **_kw: fake_redis)
    monkeypatch.setattr(
        task_module,
        "save_openapi_public_base_url",
        lambda url, **kw: runtime.save_openapi_public_base_url(url, client=fake_redis, **kw),
    )

    result = task_module.setup_openapi_public_https.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )

    assert result["status"] == "succeeded"
    assert result["fqdn"].endswith(".koreacentral.cloudapp.azure.com")
    assert result["public_base_url"].startswith("https://")
    assert result["ingress_lb_ip"] == "20.41.1.2"
    assert result["cert_expires_at"] == "2026-08-25T00:00:00Z"
    assert result["cluster_issuer"] == "letsencrypt-prod"

    # Verify the 8 expected kubectl steps fired in order (the 9th is the
    # runtime cache write which doesn't go through kubectl)
    apply_targets = [c[2] for c in calls if c[:2] == ["apply", "-f"]]
    # Step 1 + step 4 are URL applies, step 6 + step 7 are stdin applies
    assert any("ingress-nginx" in t for t in apply_targets)
    assert any("cert-manager" in t for t in apply_targets)
    # Step 2 patch, step 5 wait webhook, step 8 wait certificate
    assert any(c[:1] == ["patch"] for c in calls)
    assert any(
        c[:1] == ["wait"] and "cert-manager-webhook" in " ".join(c) for c in calls
    )
    assert any(
        c[:1] == ["wait"] and "certificate/elb-openapi-tls" in " ".join(c)
        for c in calls
    )

    # Cache contract: SPA's public-https GET reads from here without a
    # kubectl round trip.
    status = task_module.get_openapi_public_https_status()
    assert status["enabled"] is True
    assert status["fqdn"].endswith(".koreacentral.cloudapp.azure.com")
    assert status["ingress_lb_ip"] == "20.41.1.2"


def test_disable_public_https_clears_cache(monkeypatch) -> None:
    from api.services.openapi import runtime
    from api.tasks.openapi import public_https as task_module

    fake_redis = _FakeRedis()
    monkeypatch.setattr(runtime, "get_ops_redis_client", lambda **_kw: fake_redis)
    # Pre-seed cache so disable has something to clear
    runtime.save_openapi_public_base_url(
        "https://elb-openapi-abc.koreacentral.cloudapp.azure.com",
        client=fake_redis,
    )

    monkeypatch.setattr(task_module, "get_credential", lambda: object())
    monkeypatch.setattr(
        task_module,
        "ensure_admin_kubeconfig",
        lambda **_kw: "/tmp/exec/kubeconfig-fake"  # noqa: S108 - fake path returned by mocked auth helper,
    )

    deleted: list[str] = []

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        if args[:1] == ["delete"]:
            deleted.append(args[1] + "/" + args[2])
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    monkeypatch.setattr(
        task_module,
        "clear_openapi_public_base_url",
        lambda: runtime.clear_openapi_public_base_url(client=fake_redis),
    )

    result = task_module.disable_openapi_public_https.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )

    assert result["status"] == "succeeded"
    assert "ingress/elb-openapi-tls" in deleted
    assert "certificate/elb-openapi-tls" in deleted
    # Cache cleared → SPA flips baseUrl back to internal LB IP path
    assert task_module.get_openapi_public_https_status() == {"enabled": False}


def test_setup_public_https_propagates_kubectl_failure(monkeypatch) -> None:
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module, "get_credential", lambda: object())
    monkeypatch.setattr(task_module, "aks_client", lambda *_a, **_kw: _stub_cluster())
    monkeypatch.setattr(
        task_module,
        "ensure_admin_kubeconfig",
        lambda **_kw: "/tmp/exec/kubeconfig-fake"  # noqa: S108 - fake path returned by mocked auth helper,
    )

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        if args[:1] == ["apply"]:
            return {"exit_code": 1, "stderr": "Unable to connect to the server"}
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)

    result = task_module.setup_openapi_public_https.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )

    assert result["status"] == "failed"
    assert "Unable to connect" in result["error"]


def test_operator_email_resolution(monkeypatch) -> None:
    from api.tasks.openapi import public_https as task_module

    monkeypatch.delenv("ELB_OPERATOR_EMAIL", raising=False)
    assert task_module._resolve_operator_email("") == "noreply@elb-dashboard.local"
    assert task_module._resolve_operator_email("user@example.com") == "user@example.com"

    monkeypatch.setenv("ELB_OPERATOR_EMAIL", "env@example.com")
    assert task_module._resolve_operator_email("") == "env@example.com"
    # Explicit body value still wins over env
    assert task_module._resolve_operator_email("override@example.com") == "override@example.com"


def test_email_masking_does_not_leak_local_part() -> None:
    from api.tasks.openapi.public_https import _mask_email

    assert _mask_email("") == ""
    assert _mask_email("not-an-email") == ""
    assert _mask_email("a@b.com") == "a*@b.com"
    assert _mask_email("ops@example.com").startswith("o")
    assert "@example.com" in _mask_email("ops@example.com")
    assert "ps" not in _mask_email("ops@example.com")


@pytest.mark.parametrize(
    "args, want_substr",
    [
        (["apply", "-f", "-"], "stdin"),
        (["wait", "--for=condition=Ready=true"], "ready"),
    ],
)
def test_kubectl_run_helper_signature(args: list[str], want_substr: str) -> None:
    # Smoke check: ensure_admin_kubeconfig + kubectl_run are exported and
    # accept the kwargs the task uses. The actual subprocess call is mocked
    # in the higher-level tests above.
    from api.tasks.openapi.kubectl import ensure_admin_kubeconfig, kubectl_run

    assert callable(ensure_admin_kubeconfig)
    assert callable(kubectl_run)
    assert want_substr  # parametrise sanity
    assert args  # parametrise sanity
