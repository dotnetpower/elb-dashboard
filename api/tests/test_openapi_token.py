"""Tests for OpenAPI API token lifecycle helpers.

Responsibility: Tests for OpenAPI API token lifecycle helpers
Edit boundaries: Keep assertions focused on token generation, deployment patching, and runtime
cache synchronization.
Key entry points: `FakeSession`, `test_existing_openapi_token_is_returned_without_patch`,
`test_generate_openapi_token_patches_deployment_and_runtime_cache`
Risky contracts: Do not require network access, real Kubernetes credentials, or real Redis.
Validation: `uv run pytest -q api/tests/test_openapi_token.py`.
"""

from __future__ import annotations

from typing import Any


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession:
    def __init__(self, deployment: dict[str, Any]) -> None:
        self.deployment = deployment
        self.patches: list[dict[str, Any]] = []
        self.closed = False

    def get(self, _url: str, timeout: int) -> FakeResponse:
        return FakeResponse(200, self.deployment)

    def patch(
        self,
        _url: str,
        *,
        json: Any,
        headers: dict[str, str],
        timeout: int,
    ) -> FakeResponse:
        self.patches.append({"json": json, "headers": headers, "timeout": timeout})
        return FakeResponse(200, self.deployment)

    def close(self) -> None:
        self.closed = True


def _deployment(token: str = "") -> dict[str, Any]:
    env = [{"name": "ELB_CLUSTER_NAME", "value": "aks-elb"}]
    if token:
        env.append({"name": "ELB_OPENAPI_API_TOKEN", "value": token})
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "openapi",
                            "env": env,
                        }
                    ]
                }
            }
        }
    }


def test_existing_openapi_token_is_returned_without_patch(monkeypatch) -> None:
    from api.services import openapi_token

    session = FakeSession(_deployment("existing-token"))
    saved: list[str] = []
    monkeypatch.setattr(
        openapi_token,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )
    monkeypatch.setattr(
        openapi_token,
        "save_openapi_api_token",
        lambda token, **_kwargs: saved.append(token) or True,
    )

    result = openapi_token.ensure_openapi_api_token(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        regenerate=False,
    )

    assert result["configured"] is True
    assert result["token"] == "existing-token"
    assert result["generated"] is False
    assert result["rotated"] is False
    assert session.patches == []
    assert session.closed is True
    assert saved == ["existing-token"]


def test_generate_openapi_token_patches_deployment_and_runtime_cache(monkeypatch) -> None:
    from api.services import openapi_token

    session = FakeSession(_deployment())
    saved: list[str] = []
    monkeypatch.setattr(
        openapi_token,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )
    monkeypatch.setattr(openapi_token, "_generate_token", lambda: "generated-token")
    monkeypatch.setattr(
        openapi_token,
        "save_openapi_api_token",
        lambda token, **_kwargs: saved.append(token) or True,
    )
    monkeypatch.delenv("ELB_OPENAPI_API_TOKEN", raising=False)

    result = openapi_token.ensure_openapi_api_token(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        regenerate=False,
    )

    assert result["configured"] is True
    assert result["token"] == "generated-token"
    assert result["generated"] is True
    assert result["rotated"] is False
    assert saved == ["generated-token"]
    # The patch is now an RFC 6902 JSON Patch (see openapi_token._patch_deployment_token
    # docstring for the strategic-merge → JSON Patch migration rationale).
    assert session.patches[0]["headers"] == {"Content-Type": "application/json-patch+json"}
    ops = session.patches[0]["json"]
    assert isinstance(ops, list)
    # The base fake deployment has no template annotations map, so the patch
    # creates one before adding the rotated-at key.
    assert ops[0] == {
        "op": "add",
        "path": "/spec/template/metadata/annotations",
        "value": {},
    }
    assert ops[1]["op"] == "add"
    # `~1` is the JSON Pointer escape for `/` inside the annotation key
    # `elb-dashboard/openapi-api-token-rotated-at`.
    assert ops[1]["path"] == (
        "/spec/template/metadata/annotations/elb-dashboard~1openapi-api-token-rotated-at"
    )
    # The token env entry is new (existing env list only has ELB_CLUSTER_NAME),
    # so the op appends with the "-" path segment.
    token_op = ops[-1]
    assert token_op == {
        "op": "add",
        "path": "/spec/template/spec/containers/0/env/-",
        "value": {"name": "ELB_OPENAPI_API_TOKEN", "value": "generated-token"},
    }
    assert session.closed is True
