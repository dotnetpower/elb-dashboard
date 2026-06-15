"""Per-cluster outbound resolver isolation for the external-BLAST client (#26).

Responsibility: Prove that ``external_blast._base_url`` / ``_headers`` read the
    per-cluster runtime cache keys when cluster context is supplied and fall
    back to the global key otherwise, and that the primary entry points
    (``submit_job`` / ``get_job`` / ``list_jobs``) thread that context through.
Edit boundaries: Keep assertions on the resolver behaviour only; do not require
    a real Redis or a live sibling — the runtime resolvers are monkeypatched.
Key entry points: ``test_base_url_prefers_per_cluster``,
    ``test_headers_prefers_per_cluster``, ``test_entry_points_thread_context``.
Risky contracts: All new context kwargs default to ``""`` so existing call
    sites keep the global resolution. The token value is never logged.
Validation: ``uv run pytest -q api/tests/test_external_blast_cluster_resolver.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.services import external_blast
from api.services.openapi import runtime

_CLUSTER_A = {
    "subscription_id": "sub-1",
    "resource_group": "rg-a",
    "cluster_name": "aks-a",
}
_CLUSTER_B = {
    "subscription_id": "sub-1",
    "resource_group": "rg-b",
    "cluster_name": "aks-b",
}


def _arm_id(ctx: dict[str, str]) -> str:
    return (
        f"/subscriptions/{ctx['subscription_id']}/resourceGroups/{ctx['resource_group']}"
        f"/providers/Microsoft.ContainerService/managedClusters/{ctx['cluster_name']}"
    )


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The env override (`ELB_OPENAPI_BASE_URL` / `ELB_OPENAPI_API_TOKEN`) wins
    # before any cache read, so clear it to exercise the cache resolution path.
    monkeypatch.delenv("ELB_OPENAPI_BASE_URL", raising=False)
    monkeypatch.delenv("ELB_OPENAPI_API_TOKEN", raising=False)
    monkeypatch.delenv("ELB_OPENAPI_INTERNAL_TOKEN", raising=False)


def _patch_base_url_resolvers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the runtime base-url resolvers per-cluster aware in-memory."""
    per_cluster = {
        _arm_id(_CLUSTER_A).lower(): "https://a.example.com",
        _arm_id(_CLUSTER_B).lower(): "https://b.example.com",
    }

    def fake_public_tls(
        *, subscription_id: str = "", resource_group: str = "", cluster_name: str = ""
    ) -> str:
        if subscription_id and resource_group and cluster_name:
            arm = (
                f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
                f"/providers/Microsoft.ContainerService/managedClusters/{cluster_name}"
            ).lower()
            return per_cluster.get(arm, "")
        return ""

    def fake_global_base(**_kwargs: Any) -> str:
        return "http://global-runtime"

    monkeypatch.setattr(runtime, "get_public_tls_base_url", fake_public_tls)
    monkeypatch.setattr(runtime, "get_openapi_base_url", fake_global_base)


def _patch_token_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    def _ctx_key(ctx: dict[str, str]) -> tuple[str, str, str]:
        return (ctx["subscription_id"], ctx["resource_group"], ctx["cluster_name"])

    tokens = {
        _ctx_key(_CLUSTER_A): "tok-a",
        _ctx_key(_CLUSTER_B): "tok-b",
    }

    def fake_token(
        *,
        subscription_id: str = "",
        resource_group: str = "",
        cluster_name: str = "",
        client: Any | None = None,
    ) -> str:
        key = (subscription_id, resource_group, cluster_name)
        if key in tokens:
            return tokens[key]
        return "tok-global"

    monkeypatch.setattr(runtime, "get_openapi_api_token", fake_token)


def test_base_url_prefers_per_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_base_url_resolvers(monkeypatch)
    assert external_blast._base_url(**_CLUSTER_A) == "https://a.example.com"
    assert external_blast._base_url(**_CLUSTER_B) == "https://b.example.com"


def test_base_url_context_less_uses_global(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_base_url_resolvers(monkeypatch)
    assert external_blast._base_url() == "http://global-runtime"


def test_base_url_falls_back_to_global_when_no_per_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_base_url_resolvers(monkeypatch)
    # A cluster with no per-cluster public TLS entry → global runtime fallback.
    assert (
        external_blast._base_url(
            subscription_id="sub-1", resource_group="rg-z", cluster_name="aks-z"
        )
        == "http://global-runtime"
    )


def test_base_url_explicit_value_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_base_url_resolvers(monkeypatch)
    # An explicit base_url short-circuits the cache resolution entirely.
    assert external_blast._base_url("https://explicit.example.com/", **_CLUSTER_A) == (
        "https://explicit.example.com"
    )


def test_headers_prefers_per_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_token_resolver(monkeypatch)
    assert external_blast._headers(**_CLUSTER_A)["X-ELB-API-Token"] == "tok-a"
    assert external_blast._headers(**_CLUSTER_B)["X-ELB-API-Token"] == "tok-b"


def test_headers_context_less_uses_global(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_token_resolver(monkeypatch)
    assert external_blast._headers()["X-ELB-API-Token"] == "tok-global"


def test_headers_explicit_token_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_token_resolver(monkeypatch)
    assert (
        external_blast._headers(api_token="explicit-tok", **_CLUSTER_A)["X-ELB-API-Token"]
        == "explicit-tok"
    )


def test_entry_points_thread_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """``submit_job`` / ``get_job`` / ``list_jobs`` forward cluster context to
    the resolvers so an outbound call scoped to A resolves A's base URL + token."""
    recorded: list[dict[str, str]] = []

    def fake_base_url(
        value: str | None = None,
        *,
        subscription_id: str = "",
        resource_group: str = "",
        cluster_name: str = "",
    ) -> str:
        recorded.append(
            {
                "fn": "base_url",
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
            }
        )
        return "http://recorded"

    def fake_headers(
        *,
        api_token: str | None = None,
        internal_token: str | None = None,
        subscription_id: str = "",
        resource_group: str = "",
        cluster_name: str = "",
    ) -> dict[str, str]:
        recorded.append(
            {
                "fn": "headers",
                "subscription_id": subscription_id,
                "resource_group": resource_group,
                "cluster_name": cluster_name,
            }
        )
        return {"Accept": "application/json"}

    monkeypatch.setattr(external_blast, "_base_url", fake_base_url)
    monkeypatch.setattr(external_blast, "_headers", fake_headers)

    class _Resp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload
            self.status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class _Client:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        def get(self, *_args: Any, **_kwargs: Any) -> _Resp:
            return _Resp({"jobs": [], "count": 0})

        def post(self, *_args: Any, **_kwargs: Any) -> _Resp:
            return _Resp({"job_id": "abc123"})

    monkeypatch.setattr(external_blast.httpx, "Client", _Client)
    monkeypatch.setenv("OPENAPI_SUBMIT_MAX_RETRIES", "0")

    external_blast.get_job("abc123", **_CLUSTER_A)
    external_blast.list_jobs(**_CLUSTER_B)
    external_blast.submit_job({"db": "core_nt"}, **_CLUSTER_A)

    # Every resolver call carried the cluster context it was given.
    base_calls = [r for r in recorded if r["fn"] == "base_url"]
    header_calls = [r for r in recorded if r["fn"] == "headers"]
    assert base_calls, "base_url resolver was never called"
    assert header_calls, "headers resolver was never called"
    a_ctx = {k: _CLUSTER_A[k] for k in ("subscription_id", "resource_group", "cluster_name")}
    b_ctx = {k: _CLUSTER_B[k] for k in ("subscription_id", "resource_group", "cluster_name")}
    seen = {
        (r["subscription_id"], r["resource_group"], r["cluster_name"]) for r in recorded
    }
    assert (a_ctx["subscription_id"], a_ctx["resource_group"], a_ctx["cluster_name"]) in seen
    assert (b_ctx["subscription_id"], b_ctx["resource_group"], b_ctx["cluster_name"]) in seen
