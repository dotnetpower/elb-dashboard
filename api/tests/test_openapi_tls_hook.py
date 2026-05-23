"""TLS endpoint hook tests for the OpenAPI proxy and spec routes.

Responsibility: Verify the `OPENAPI_PUBLIC_BASE_URL` env hook is a strict no-op
    when empty (legacy IP path runs unchanged) and that when set to an HTTPS URL
    the proxy / spec / external-jobs callers route via the public endpoint and
    bypass the `_is_private_ipv4` admin-token guard (TLS protects in transit).
Edit boundaries: Test-only. If a new caller starts to honor the public TLS hook,
    add an explicit assertion here so accidental regressions surface immediately.
Key entry points: `test_*` functions.
Risky contracts: These tests do NOT exercise real cluster IP discovery — they
    monkeypatch `k8s_get_service_ip` so the legacy branch returns a deterministic
    answer. The point is to assert the env hook routes correctly, not to test the
    cluster path.
Validation: `uv run pytest -q api/tests/test_openapi_tls_hook.py`.
"""

from __future__ import annotations

import pytest
from api.services.openapi.runtime import get_public_tls_base_url


def test_get_public_tls_base_url_returns_empty_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAPI_PUBLIC_BASE_URL", raising=False)
    assert get_public_tls_base_url() == ""


def test_get_public_tls_base_url_normalises_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAPI_PUBLIC_BASE_URL", "https://openapi.example.com/")
    assert get_public_tls_base_url() == "https://openapi.example.com"


def test_external_jobs_kwargs_uses_public_tls_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the env is set, the cluster IP lookup must be skipped."""
    monkeypatch.setenv("OPENAPI_PUBLIC_BASE_URL", "https://openapi.example.com")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "test-token")

    from api.services.blast import external_jobs

    external_jobs._reset_external_jobs_cache()

    called: dict[str, int] = {"k8s_ip": 0, "k8s_env": 0}

    def fake_get_service_ip(*args: object, **kwargs: object) -> str | None:
        called["k8s_ip"] += 1
        return None  # If this gets called we want the test to fail clearly.

    def fake_get_env(*args: object, **kwargs: object) -> str:
        called["k8s_env"] += 1
        return "token-from-cluster"

    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_get_service_ip", fake_get_service_ip
    )
    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_get_deployment_env_value", fake_get_env
    )
    # Credential is required by the function even though we won't use it.
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    kwargs = external_jobs._openapi_client_kwargs_from_cluster(
        "sub-1", "rg-1", "cluster-1"
    )

    assert kwargs["base_url"] == "https://openapi.example.com"
    # Token was already in env → cluster env lookup not needed.
    assert kwargs["api_token"] == "test-token"
    assert called["k8s_ip"] == 0, "public TLS hook must skip IP lookup"


def test_external_jobs_kwargs_falls_back_to_ip_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the env is unset, the legacy IP-based code path must run unchanged."""
    monkeypatch.delenv("OPENAPI_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "test-token")

    from api.services.blast import external_jobs

    external_jobs._reset_external_jobs_cache()

    called: dict[str, int] = {"k8s_ip": 0}

    def fake_get_service_ip(*args: object, **kwargs: object) -> str | None:
        called["k8s_ip"] += 1
        return "10.20.30.40"

    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_get_service_ip", fake_get_service_ip
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    kwargs = external_jobs._openapi_client_kwargs_from_cluster(
        "sub-1", "rg-1", "cluster-1"
    )

    assert kwargs["base_url"] == "http://10.20.30.40"
    assert called["k8s_ip"] == 1, "legacy path must hit k8s_get_service_ip"


def test_external_jobs_kwargs_uses_public_tls_when_cluster_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public TLS endpoint is reachable independently of the K8s API.

    If `k8s_get_service_ip` raises (e.g. the operator's az login expired,
    or the AKS API server is temporarily unhealthy), the dashboard should
    still be able to talk to the sibling service over the public TLS LB.
    """
    monkeypatch.setenv("OPENAPI_PUBLIC_BASE_URL", "https://openapi.example.com")
    monkeypatch.setenv("ELB_OPENAPI_API_TOKEN", "test-token")

    from api.services.blast import external_jobs

    external_jobs._reset_external_jobs_cache()

    def angry_get_service_ip(*args: object, **kwargs: object) -> str | None:
        raise RuntimeError("AKS API server unhealthy")

    monkeypatch.setattr(
        "api.services.k8s.monitoring.k8s_get_service_ip", angry_get_service_ip
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())

    kwargs = external_jobs._openapi_client_kwargs_from_cluster(
        "sub-1", "rg-1", "cluster-1"
    )

    assert kwargs.get("base_url") == "https://openapi.example.com"
