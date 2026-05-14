"""Tests for HTTP boundary hardening helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _http_utils import _azure_error_response


def _body(response) -> dict[str, str]:
    return json.loads(response.get_body().decode("utf-8"))


def test_azure_error_response_maps_not_found() -> None:
    response = _azure_error_response(ResourceNotFoundError("missing"), operation="Storage")

    assert response.status_code == 404
    assert _body(response)["error"] == "Storage not found"


def test_azure_error_response_maps_network_failure_to_503() -> None:
    response = _azure_error_response(ServiceRequestError("connection reset"), operation="AKS")

    assert response.status_code == 503
    assert _body(response)["error"] == "AKS temporarily unavailable"


def test_azure_error_response_sanitises_client_error() -> None:
    error = HttpResponseError(
        "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.payloadhere"
    )
    error.status_code = 400
    response = _azure_error_response(error, operation="ACR")

    assert response.status_code == 400
    body = _body(response)
    assert "eyJ0" not in body["error"]
    assert "<redacted>" in body["error"]
