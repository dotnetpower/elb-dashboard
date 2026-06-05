"""Unit tests for `_classify_exception` and `_graceful` reason codes.

Responsibility: Lock in the degraded-reason taxonomy returned by the monitor router so the
SPA's diagnostics banner branches stay consistent with the backend.
Edit boundaries: Only exercise pure functions in `api.routes.monitor.common`; no FastAPI app
or Azure SDK is required.
Key entry points: `test_classify_*`, `test_graceful_returns_dict_with_reason`,
`test_graceful_wraps_non_dict_empty`.
Risky contracts: `degraded_reason` strings are part of the API contract consumed by the SPA
(see `web/src/utils/monitorDegraded.ts`). Renaming a code here without updating the SPA will
silently break the diagnostics banner classification.
Validation: `uv run pytest -q api/tests/test_monitor_graceful.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from api.routes.monitor.common import _classify_exception, _graceful
from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ResourceNotFoundError,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.headers: dict[str, str] = {}
        self.reason = "Fake"

    def text(self, encoding: str | None = None) -> str:
        return self._body


def _http_error(status: int, message: str = "boom") -> HttpResponseError:
    err = HttpResponseError(message=message, response=_FakeResponse(status, message))
    err.status_code = status  # type: ignore[attr-defined]
    return err


def _http_error_with_code(status: int, code: str, message: str = "boom") -> HttpResponseError:
    """Build an HttpResponseError whose `.error.code` is populated, matching
    what azure-core attaches when the response body carries an ARM error
    envelope. The classifier should prefer this over substring matching.
    """

    class _Err:
        def __init__(self, code: str) -> None:
            self.code = code
            self.message = message

    err = _http_error(status, message)
    err.error = _Err(code)  # type: ignore[attr-defined]
    return err


def test_classify_resource_not_found_maps_to_not_found() -> None:
    assert _classify_exception(ResourceNotFoundError("missing")) == "not_found"


def test_classify_403_maps_to_forbidden() -> None:
    assert _classify_exception(_http_error(403)) == "forbidden"


def test_classify_404_maps_to_not_found() -> None:
    assert _classify_exception(_http_error(404)) == "not_found"


def test_classify_401_generic_maps_to_unauthorized() -> None:
    assert _classify_exception(_http_error(401, "Bearer invalid")) == "unauthorized"


def test_classify_401_wrong_tenant_marker_maps_to_auth_wrong_tenant() -> None:
    msg = (
        "(InvalidAuthenticationTokenTenant) The access token is from the wrong "
        "issuer 'https://sts.windows.net/aaaa/'."
    )
    assert _classify_exception(_http_error(401, msg)) == "auth_wrong_tenant"


def test_classify_401_aadsts_signin_pattern_maps_to_auth_wrong_tenant() -> None:
    msg = "AADSTS50020: User account does not exist in tenant"
    assert _classify_exception(_http_error(401, msg)) == "auth_wrong_tenant"


def test_classify_401_with_structured_code_wins_over_message() -> None:
    """When ``error.code`` is set, the classifier must trust it even if the
    message body has been reworded by Azure."""
    exc = _http_error_with_code(401, "InvalidAuthenticationTokenTenant", "anything")
    assert _classify_exception(exc) == "auth_wrong_tenant"


def test_classify_401_invalid_authentication_token_code_maps_to_wrong_tenant() -> None:
    exc = _http_error_with_code(401, "InvalidAuthenticationToken", "anything")
    assert _classify_exception(exc) == "auth_wrong_tenant"


def test_classify_401_authorization_failed_code_without_marker_is_plain_unauthorized() -> None:
    """``AuthorizationFailed`` alone is too broad — only treat it as
    wrong-tenant when the message also names the issuer marker."""
    exc = _http_error_with_code(401, "AuthorizationFailed", "missing role")
    assert _classify_exception(exc) == "unauthorized"


def test_classify_401_authorization_failed_with_marker_is_wrong_tenant() -> None:
    exc = _http_error_with_code(
        401,
        "AuthorizationFailed",
        "AADSTS50020: user not in tenant",
    )
    assert _classify_exception(exc) == "auth_wrong_tenant"


def test_classify_other_http_status_falls_back_to_http_code() -> None:
    assert _classify_exception(_http_error(500)) == "http_500"


def test_classify_azure_error_without_status_maps_to_azure_error() -> None:
    assert _classify_exception(AzureError("generic")) == "azure_error"


def test_classify_unknown_exception_uses_class_name() -> None:
    assert _classify_exception(RuntimeError("nope")) == "RuntimeError"


def test_graceful_returns_dict_with_reason_when_empty_is_dict() -> None:
    out: Any = _graceful("aks_list", _http_error(403), empty={"clusters": []})
    assert out == {"clusters": [], "degraded": True, "degraded_reason": "forbidden"}


def test_graceful_wraps_non_dict_empty_into_items_dict() -> None:
    out: Any = _graceful("items", ResourceNotFoundError("nope"), empty=[1, 2, 3])
    assert out == {"items": [1, 2, 3], "degraded": True, "degraded_reason": "not_found"}


def test_graceful_propagates_wrong_tenant_reason() -> None:
    err = _http_error(
        401,
        "(InvalidAuthenticationTokenTenant) The access token is from the wrong issuer",
    )
    out: Any = _graceful("storage_summary", err, empty={"containers": []})
    assert out["degraded"] is True
    assert out["degraded_reason"] == "auth_wrong_tenant"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (500, "http_500"),
    ],
)
def test_graceful_status_table(status: int, expected: str) -> None:
    out: Any = _graceful("op", _http_error(status), empty={"items": []})
    assert out["degraded_reason"] == expected


def test_graceful_increments_degraded_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every degraded response MUST increment the OTel route-degraded counter,
    labelled by op + reason, so operators can alert on user-visible monitor
    degradation independently of the cache-layer loader-failure counter."""
    from api.routes.monitor import common as common_module

    common_module._reset_degraded_counter()
    recorded: list[tuple[int, dict[str, object]]] = []

    class _RecordingCounter:
        def add(self, value: int, attributes: dict[str, object] | None = None) -> None:
            recorded.append((value, dict(attributes or {})))

    monkeypatch.setattr(common_module, "_get_degraded_counter", lambda: _RecordingCounter())

    _graceful("aks_list", _http_error(403), empty={"clusters": []})

    assert recorded == [(1, {"op": "aks_list", "reason": "forbidden"})]


def test_graceful_counter_failure_never_breaks_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken meter must not turn a graceful degrade into a 500."""
    from api.routes.monitor import common as common_module

    common_module._reset_degraded_counter()

    class _ExplodingCounter:
        def add(self, value: int, attributes: dict[str, object] | None = None) -> None:
            raise RuntimeError("meter exporter down")

    monkeypatch.setattr(common_module, "_get_degraded_counter", lambda: _ExplodingCounter())

    out: Any = _graceful("aks_list", _http_error(403), empty={"clusters": []})
    assert out == {"clusters": [], "degraded": True, "degraded_reason": "forbidden"}

