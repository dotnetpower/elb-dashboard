"""Tests for OpenAPI deployment status helpers.

Responsibility: Tests for OpenAPI deployment status helpers
Edit boundaries: Keep assertions focused on Kubernetes deployment image inspection.
Key entry points: `FakeSession`, `test_openapi_deployment_status_extracts_image_tag`
Risky contracts: Do not require network access, real Kubernetes credentials, or real Azure.
Validation: `uv run pytest -q api/tests/test_openapi_deployment.py`.
"""

from __future__ import annotations

from typing import Any


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession:
    def __init__(self, deployment: dict[str, Any]) -> None:
        self.deployment = deployment
        self.closed = False

    def get(self, _url: str, timeout: int) -> FakeResponse:
        return FakeResponse(200, self.deployment)

    def close(self) -> None:
        self.closed = True


def test_openapi_deployment_status_extracts_image_tag(monkeypatch) -> None:
    from api.services import openapi_deployment

    session = FakeSession(
        {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "openapi",
                                "image": "elbacr.azurecr.io/elb-openapi:4.9",
                            }
                        ]
                    }
                }
            }
        }
    )
    monkeypatch.setattr(
        openapi_deployment,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )

    result = openapi_deployment.get_openapi_deployment_status(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )

    assert result == {
        "configured": True,
        "deployment_name": "elb-openapi",
        "container_name": "openapi",
        "namespace": "default",
        "image": "elbacr.azurecr.io/elb-openapi:4.9",
        "image_repository": "elbacr.azurecr.io/elb-openapi",
        "image_tag": "4.9",
    }
    assert session.closed is True
