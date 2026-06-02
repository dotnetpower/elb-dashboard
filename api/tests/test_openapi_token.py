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
    from api.services.openapi import token as openapi_token

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
    from api.services.openapi import token as openapi_token

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


def test_status_returns_existing_token_without_patch(monkeypatch) -> None:
    from api.services.openapi import token as openapi_token

    session = FakeSession(_deployment("live-token"))
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

    result = openapi_token.get_openapi_api_token_status(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )

    assert result["configured"] is True
    assert result["token"] == "live-token"
    assert result["generated"] is False
    assert result["updated_at"] is None
    assert result["self_heal_error"] is None
    assert session.patches == []
    assert saved == ["live-token"]


def test_status_self_heals_legacy_deployment_without_token_env(monkeypatch) -> None:
    """Pre-9d4e549 deployments shipped without `ELB_OPENAPI_API_TOKEN`.

    The status endpoint must mint + patch one in place so the SPA panel does
    not stay on "No API token generated" until the operator clicks Generate.
    """
    from api.services.openapi import token as openapi_token

    session = FakeSession(_deployment())
    saved: list[str] = []
    audit_events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        openapi_token,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )
    monkeypatch.setattr(openapi_token, "_generate_token", lambda: "auto-healed-token")
    monkeypatch.setattr(
        openapi_token,
        "save_openapi_api_token",
        lambda token, **_kwargs: saved.append(token) or True,
    )
    monkeypatch.setattr(
        openapi_token,
        "_record_self_heal_audit",
        lambda *, event, detail, **_kwargs: audit_events.append((event, detail)),
    )
    monkeypatch.delenv("ELB_OPENAPI_API_TOKEN", raising=False)

    result = openapi_token.get_openapi_api_token_status(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )

    assert result["configured"] is True
    assert result["token"] == "auto-healed-token"
    assert result["generated"] is True
    assert result["updated_at"] is not None
    assert result["self_heal_error"] is None
    assert saved == ["auto-healed-token"]
    # Audit row emitted with the success event so /api/audit/log picks it up.
    assert len(audit_events) == 1
    event_name, event_detail = audit_events[0]
    assert event_name == "openapi_token_self_healed"
    assert event_detail["deployment_name"] == openapi_token.OPENAPI_DEPLOYMENT_NAME
    # A JSON Patch went out — the self-heal path reuses _patch_deployment_token.
    assert len(session.patches) == 1
    assert session.patches[0]["headers"] == {"Content-Type": "application/json-patch+json"}
    ops = session.patches[0]["json"]
    token_op = ops[-1]
    assert token_op == {
        "op": "add",
        "path": "/spec/template/spec/containers/0/env/-",
        "value": {"name": "ELB_OPENAPI_API_TOKEN", "value": "auto-healed-token"},
    }


def test_status_self_heal_patch_failure_falls_back_to_empty(monkeypatch) -> None:
    """If the env is empty but the patch fails (RBAC / webhook), the status
    must report `configured=false` AND surface the failure code+message in
    `self_heal_error` so the SPA panel can render an actionable red banner
    (not the silent "No API token generated" placeholder).
    """
    from api.services.openapi import token as openapi_token

    session = FakeSession(_deployment())
    audit_events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        openapi_token,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )
    monkeypatch.setattr(openapi_token, "_generate_token", lambda: "would-be-token")
    monkeypatch.setattr(
        openapi_token,
        "_record_self_heal_audit",
        lambda *, event, detail, **_kwargs: audit_events.append((event, detail)),
    )

    def _explode(*_args, **_kwargs):
        raise openapi_token.OpenApiTokenError(502, "openapi_token_patch_failed", "boom")

    monkeypatch.setattr(openapi_token, "_patch_deployment_token", _explode)

    result = openapi_token.get_openapi_api_token_status(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )

    assert result["configured"] is False
    assert result["token"] == ""
    assert result["generated"] is False
    assert result["updated_at"] is None
    assert result["self_heal_error"] == {
        "code": "openapi_token_patch_failed",
        "message": "boom",
    }
    # Audit row carries the failure event with the K8s error fields so
    # operators can correlate from the SPA audit table without re-reading
    # api sidecar logs.
    assert len(audit_events) == 1
    event_name, event_detail = audit_events[0]
    assert event_name == "openapi_token_self_heal_failed"
    assert event_detail["error_code"] == "openapi_token_patch_failed"
    assert event_detail["error_message"] == "boom"


def test_self_heal_paths_never_leak_token_value(monkeypatch) -> None:
    """Hardening contract: neither the audit payload nor the log message
    may ever carry the minted token value. A regression here would ship
    OpenAPI admin tokens into the audit table / Log Analytics, which is
    a serious credential leak."""
    from api.services.openapi import token as openapi_token

    audit_events: list[tuple[str, dict[str, Any]]] = []
    secret_token = "DO-NOT-LEAK-this-very-secret-token-value"
    session = FakeSession(_deployment())
    monkeypatch.setattr(
        openapi_token,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )
    monkeypatch.setattr(openapi_token, "_generate_token", lambda: secret_token)
    monkeypatch.setattr(
        openapi_token,
        "save_openapi_api_token",
        lambda token, **_kwargs: True,
    )
    monkeypatch.setattr(
        openapi_token,
        "_record_self_heal_audit",
        lambda *, event, detail, **_kwargs: audit_events.append((event, detail)),
    )
    monkeypatch.delenv("ELB_OPENAPI_API_TOKEN", raising=False)

    result = openapi_token.get_openapi_api_token_status(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )

    # Sanity: the test actually exercised the self-heal mint path.
    assert result["token"] == secret_token
    assert result["generated"] is True
    # The audit payload must never carry the token value (search every
    # nested value, not just top-level keys, so a future refactor that
    # adds a sub-dict cannot quietly bypass this guard).
    assert len(audit_events) == 1
    _, detail = audit_events[0]

    def _flatten(value: Any) -> list[str]:
        if isinstance(value, dict):
            out: list[str] = []
            for v in value.values():
                out.extend(_flatten(v))
            return out
        if isinstance(value, list | tuple):
            out = []
            for item in value:
                out.extend(_flatten(item))
            return out
        return [str(value)]

    for blob in _flatten(detail):
        assert secret_token not in blob, (
            f"audit detail leaked the OpenAPI token via value: {blob!r}"
        )


def test_self_heal_is_idempotent_when_env_already_populated(monkeypatch) -> None:
    """Once a self-heal mint has populated the deployment env, subsequent
    status calls must be pure reads — no second mint, no second audit row.
    This guards against the auto-mint becoming a silent rotator if some
    future refactor accidentally re-triggers the empty-env branch."""
    from api.services.openapi import token as openapi_token

    # First call: env empty → self-heal mints "minted-by-heal".
    # Subsequent call (simulated by a fresh FakeSession that reflects the
    # post-patch deployment) reads the live token and must not mint again.
    session_after = FakeSession(_deployment("minted-by-heal"))
    audit_events: list[str] = []
    mint_count = {"n": 0}

    def _counting_generate() -> str:
        mint_count["n"] += 1
        return f"unexpected-extra-mint-{mint_count['n']}"

    monkeypatch.setattr(
        openapi_token,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (session_after, "https://k8s"),
    )
    monkeypatch.setattr(openapi_token, "_generate_token", _counting_generate)
    monkeypatch.setattr(
        openapi_token,
        "save_openapi_api_token",
        lambda token, **_kwargs: True,
    )
    monkeypatch.setattr(
        openapi_token,
        "_record_self_heal_audit",
        lambda *, event, **_kwargs: audit_events.append(event),
    )

    result = openapi_token.get_openapi_api_token_status(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
    )

    assert result["token"] == "minted-by-heal"
    assert result["generated"] is False
    assert result["self_heal_error"] is None
    assert mint_count["n"] == 0, (
        "self-heal must not run when the env entry is already populated"
    )
    assert audit_events == [], (
        "no audit row must be written for read-only status checks"
    )
    assert session_after.patches == [], "no PATCH must be issued on read-only path"


def test_self_heal_audit_inherits_caller_identity(monkeypatch) -> None:
    """Audit rows must carry the triggering caller's oid/tenant so
    `/api/audit/log` (which queries `list_for_owner(caller.object_id)`)
    surfaces the event to the user. Without this the row would land
    under `owner_oid="system"` and never appear in the SPA audit table.
    """
    from api.services.openapi import token as openapi_token

    session = FakeSession(_deployment())
    captured: dict[str, Any] = {}

    def _capture(*, event, detail, caller_oid="", tenant_id="", **_kwargs):
        captured["event"] = event
        captured["detail"] = detail
        captured["caller_oid"] = caller_oid
        captured["tenant_id"] = tenant_id

    monkeypatch.setattr(
        openapi_token,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )
    monkeypatch.setattr(openapi_token, "_generate_token", lambda: "minted-with-caller")
    monkeypatch.setattr(
        openapi_token,
        "save_openapi_api_token",
        lambda token, **_kwargs: True,
    )
    monkeypatch.setattr(openapi_token, "_record_self_heal_audit", _capture)

    openapi_token.get_openapi_api_token_status(
        object(),
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        caller_oid="caller-oid-abc123",
        tenant_id="tenant-xyz",
    )

    assert captured["caller_oid"] == "caller-oid-abc123"
    assert captured["tenant_id"] == "tenant-xyz"
    assert captured["event"] == "openapi_token_self_healed"


def test_resync_from_cluster_reads_pod_token_and_syncs(monkeypatch) -> None:
    """The reactive 401 self-heal re-reads the live ELB_OPENAPI_API_TOKEN
    from the elb-openapi deployment env and syncs it into the runtime cache
    using the cluster context cached alongside the OpenAPI base URL. It must
    NEVER mint a token — a 401 means the pod already holds one."""
    from api.services.openapi import token as openapi_token

    session = FakeSession(_deployment("pod-live-token"))
    synced: list[tuple[str, dict[str, Any]]] = []
    mint_count = {"n": 0}

    monkeypatch.setattr(
        "api.services.openapi.runtime.get_openapi_runtime_metadata",
        lambda: {
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
        },
    )
    monkeypatch.setattr(
        "api.services.get_credential",
        lambda: object(),
    )
    monkeypatch.setattr(
        openapi_token,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )

    def _generate_must_not_run() -> str:
        mint_count["n"] += 1
        return "unexpected-mint"

    monkeypatch.setattr(openapi_token, "_generate_token", _generate_must_not_run)
    monkeypatch.setattr(
        openapi_token,
        "_sync_runtime_token",
        lambda token, metadata: synced.append((token, metadata)),
    )

    result = openapi_token.resync_openapi_api_token_from_cluster()

    assert result == "pod-live-token"
    assert mint_count["n"] == 0, "resync must never mint a token"
    assert len(synced) == 1
    token, metadata = synced[0]
    assert token == "pod-live-token"
    assert metadata["source"] == "token_resync_on_401"
    assert metadata["subscription_id"] == "sub-1"
    assert metadata["cluster_name"] == "aks-elb"
    assert session.closed is True


def test_resync_from_cluster_skips_without_context(monkeypatch) -> None:
    """No cached cluster context → return "" without touching K8s or the
    cache. Best-effort by contract."""
    from api.services.openapi import token as openapi_token

    called = {"k8s": False, "sync": False}
    monkeypatch.setattr(
        "api.services.openapi.runtime.get_openapi_runtime_metadata",
        lambda: {},
    )

    def _must_not_call(*_args, **_kwargs):
        called["k8s"] = True
        raise AssertionError("k8s session must not be created without context")

    monkeypatch.setattr(openapi_token, "_get_k8s_session", _must_not_call)
    monkeypatch.setattr(
        openapi_token,
        "_sync_runtime_token",
        lambda *_args, **_kwargs: called.__setitem__("sync", True),
    )

    result = openapi_token.resync_openapi_api_token_from_cluster()

    assert result == ""
    assert called == {"k8s": False, "sync": False}


def test_resync_from_cluster_returns_empty_when_pod_has_no_token_env(monkeypatch) -> None:
    """If the elb-openapi deployment env has no ELB_OPENAPI_API_TOKEN entry
    there is nothing to resync — return "" and never mint."""
    from api.services.openapi import token as openapi_token

    session = FakeSession(_deployment())  # no token env entry
    synced: list[Any] = []
    monkeypatch.setattr(
        "api.services.openapi.runtime.get_openapi_runtime_metadata",
        lambda: {
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
        },
    )
    monkeypatch.setattr("api.services.get_credential", lambda: object())
    monkeypatch.setattr(
        openapi_token,
        "_get_k8s_session",
        lambda *_args, **_kwargs: (session, "https://k8s"),
    )
    monkeypatch.setattr(
        openapi_token,
        "_sync_runtime_token",
        lambda *_args, **_kwargs: synced.append(True),
    )

    result = openapi_token.resync_openapi_api_token_from_cluster()

    assert result == ""
    assert synced == []
    assert session.closed is True
