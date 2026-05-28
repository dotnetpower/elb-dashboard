"""Tests for `/api/aks/autostop*` routes.

Responsibility: HTTP-level coverage of GET / PUT / POST extend / GET status.
    Stubs the evaluator + power-state lookup so the route layer is exercised
    without Azure SDK calls.
Edit boundaries: Routes only — the evaluator and storage code have their
    own dedicated tests.
Key entry points: see per-test docstrings.
Risky contracts: Response shape (`enabled`, `idle_minutes`, `verdict`,
    `next_stop_at`, …) is part of the SPA banner contract — locked here.
Validation: `uv run pytest -q api/tests/test_aks_autostop_route.py`.
"""

from __future__ import annotations

import pytest
from api.services.auto_stop import AutoStopPreference
from api.services.auto_stop_evaluator import IdleDecision
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("API_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))
    # `/api/aks/autostop/status` calls `get_state_repo()` inside the route
    # body; stub at the source module so the route works without a real
    # Table endpoint. Individual tests can override.
    monkeypatch.setattr(
        "api.services.state_repo.get_state_repo", lambda: object()
    )
    from api.main import app

    return TestClient(app)


def _qs() -> str:
    return "subscription_id=sub-1&resource_group=rg-elb&cluster_name=elb-cluster"


def test_get_autostop_returns_default_when_no_pref(client: TestClient) -> None:
    resp = client.get(f"/api/aks/autostop?{_qs()}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["exists"] is False
    assert body["idle_minutes"] == 60
    assert body["allowed_idle_minutes"] == [15, 30, 60, 120, 240]


def test_put_autostop_persists_preference(client: TestClient) -> None:
    resp = client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 30,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["idle_minutes"] == 30
    assert body["exists"] is True

    # Re-GET reflects the saved value.
    follow = client.get(f"/api/aks/autostop?{_qs()}").json()
    assert follow["enabled"] is True
    assert follow["idle_minutes"] == 30


def test_put_autostop_rejects_invalid_idle_minutes(client: TestClient) -> None:
    """`17` is not an allowed bucket — route returns 400 with a clear contract message
    instead of silently clamping the value (which was the previous, surprising behaviour)."""
    resp = client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 17,
        },
    )
    assert resp.status_code == 400
    assert "idle_minutes" in resp.json()["detail"]


def test_extend_returns_404_when_no_pref(client: TestClient) -> None:
    resp = client.post(
        "/api/aks/autostop/extend",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "minutes": 30,
        },
    )
    assert resp.status_code == 404


def test_extend_pushes_deadline(client: TestClient) -> None:
    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 60,
        },
    )
    resp = client.post(
        "/api/aks/autostop/extend",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "minutes": 30,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["extend_until"] != ""


def test_status_disabled_when_no_pref(client: TestClient) -> None:
    resp = client.get(f"/api/aks/autostop/status?{_qs()}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["exists"] is False
    assert body["verdict"] == "disabled"
    assert body["reason"] == "no_preference"


def test_status_returns_verdict_from_evaluator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status route asks the evaluator and forwards its verdict + power state."""
    # Persist an enabled pref.
    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 60,
        },
    )

    captured: dict[str, object] = {}

    def fake_evaluate(pref: AutoStopPreference, *, repo, power_state: str = "") -> IdleDecision:
        captured["pref_enabled"] = pref.enabled
        captured["power_state"] = power_state
        return IdleDecision(
            verdict="warn",
            reason="idle_pending",
            next_stop_at="2026-05-29T13:00:00+00:00",
            seconds_until_stop=900,
            active_job_count=0,
            cluster_power_state=power_state,
        )

    monkeypatch.setattr("api.routes.aks.autostop.evaluate_cluster", fake_evaluate)
    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        lambda *_a, **_kw: {"power_state": "Running", "healthy": True},
    )

    resp = client.get(f"/api/aks/autostop/status?{_qs()}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["exists"] is True
    assert body["enabled"] is True
    assert body["verdict"] == "warn"
    assert body["reason"] == "idle_pending"
    assert body["seconds_until_stop"] == 900
    assert body["cluster_power_state"] == "Running"
    assert captured["pref_enabled"] is True
    assert captured["power_state"] == "Running"


def test_status_disabled_overrides_verdict_when_pref_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled pref must report verdict='disabled' even if evaluator says warn."""
    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": False,
            "idle_minutes": 60,
        },
    )
    monkeypatch.setattr(
        "api.routes.aks.autostop.evaluate_cluster",
        lambda pref, *, repo, power_state="": IdleDecision(
            verdict="warn", reason="idle_pending"
        ),
    )
    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        lambda *_a, **_kw: {"power_state": "Running", "healthy": True},
    )

    body = client.get(f"/api/aks/autostop/status?{_qs()}").json()
    assert body["enabled"] is False
    assert body["verdict"] == "disabled"


def test_put_autostop_strips_owner_oid_from_response(client: TestClient) -> None:
    """Response must not include `owner_oid` / `tenant_id`."""
    resp = client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 60,
        },
    )
    body = resp.json()
    assert "owner_oid" not in body
    assert "tenant_id" not in body


def test_extend_caps_at_4h(client: TestClient) -> None:
    """`minutes > 240` (4 h) is rejected at the route boundary."""
    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 60,
        },
    )
    resp = client.post(
        "/api/aks/autostop/extend",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "minutes": 24 * 60,  # 24 h
        },
    )
    assert resp.status_code == 422  # Pydantic rejects via Field(le=MAX_EXTEND_MINUTES)


def test_status_is_cached_between_polls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status route caches the evaluator output for ~30 s to absorb SPA polling."""
    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 60,
        },
    )
    call_count = {"n": 0}

    def fake_evaluate(pref, *, repo, power_state="") -> IdleDecision:
        call_count["n"] += 1
        return IdleDecision(
            verdict="keep", reason="active", cluster_power_state=power_state
        )

    monkeypatch.setattr("api.routes.aks.autostop.evaluate_cluster", fake_evaluate)
    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        lambda *_a, **_kw: {"power_state": "Running", "healthy": True},
    )

    for _ in range(5):
        client.get(f"/api/aks/autostop/status?{_qs()}")
    # All five polls must hit the cache after the first.
    assert call_count["n"] == 1


def test_put_invalidates_status_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PUT must invalidate the cached status so the SPA sees the new state immediately."""
    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 60,
        },
    )
    call_count = {"n": 0}
    monkeypatch.setattr(
        "api.routes.aks.autostop.evaluate_cluster",
        lambda pref, *, repo, power_state="": (
            call_count.__setitem__("n", call_count["n"] + 1)  # type: ignore[func-returns-value]
            or IdleDecision(verdict="keep", reason="active")
        ),
    )
    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        lambda *_a, **_kw: {"power_state": "Running", "healthy": True},
    )

    client.get(f"/api/aks/autostop/status?{_qs()}")
    assert call_count["n"] == 1
    # Second poll within TTL → cached.
    client.get(f"/api/aks/autostop/status?{_qs()}")
    assert call_count["n"] == 1
    # PUT must drop the cache.
    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 30,
        },
    )
    client.get(f"/api/aks/autostop/status?{_qs()}")
    assert call_count["n"] == 2


def test_put_rejects_cross_owner_modification(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pref owned by another real user cannot be modified by a different
    real caller. Dev-bypass is explicitly exempted because it represents
    a local-only operator account, so this test disables that exemption
    to exercise the production path through the TestClient."""
    from api.services.auto_stop import AutoStopPreference, save_auto_stop_preference

    save_auto_stop_preference(
        AutoStopPreference(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            enabled=True,
            idle_minutes=60,
            owner_oid="other-user-oid",
            tenant_id="other-tenant",
        )
    )
    # The TestClient uses AUTH_DEV_BYPASS which legitimately owns every
    # row via `is_dev_bypass_caller`. Disable that exemption so the test
    # exercises the cross-owner refusal a production caller would hit.
    import api.routes.aks.autostop as route

    monkeypatch.setattr(route, "is_dev_bypass_caller", lambda _caller: False)

    resp = client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": False,
            "idle_minutes": 60,
        },
    )
    assert resp.status_code == 403


def test_get_redacts_foreign_owner_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GET /autostop` returns the empty/default shape (not the foreign
    bookkeeping fields) when the row is owned by a different user.
    Prevents idle-pattern leakage in multi-user deployments."""
    from api.services.auto_stop import AutoStopPreference, save_auto_stop_preference

    save_auto_stop_preference(
        AutoStopPreference(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            enabled=True,
            idle_minutes=60,
            owner_oid="other-user-oid",
            last_stop_at="2026-05-29T10:00:00+00:00",
            last_stop_reason="idle:60m",
        )
    )
    import api.routes.aks.autostop as route

    monkeypatch.setattr(route, "is_dev_bypass_caller", lambda _caller: False)

    body = client.get(f"/api/aks/autostop?{_qs()}").json()
    assert body["exists"] is False
    assert body["editable"] is False
    assert body["enabled"] is False
    # Foreign caller MUST NOT see when the cluster was last stopped or why.
    assert body["last_stop_at"] == ""
    assert body["last_stop_reason"] == ""


def test_get_redacts_foreign_owner_in_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same redaction rule applies to `/autostop/status` — a foreign
    caller sees `verdict='disabled'` instead of the cross-user verdict."""
    from api.services.auto_stop import AutoStopPreference, save_auto_stop_preference

    save_auto_stop_preference(
        AutoStopPreference(
            subscription_id="sub-1",
            resource_group="rg-elb",
            cluster_name="elb-cluster",
            enabled=True,
            idle_minutes=60,
            owner_oid="other-user-oid",
        )
    )
    import api.routes.aks.autostop as route

    monkeypatch.setattr(route, "is_dev_bypass_caller", lambda _caller: False)

    body = client.get(f"/api/aks/autostop/status?{_qs()}").json()
    assert body["exists"] is False
    assert body["editable"] is False
    assert body["verdict"] == "disabled"
    assert body["reason"] == "no_preference"


def test_status_does_not_cache_degraded_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient evaluator failures must NOT be cached for 30s — the
    next poll has to re-attempt or the SPA banner sticks on a stale
    `state_repo_unreachable`."""
    from api.services.auto_stop_evaluator import IdleDecision

    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 60,
        },
    )
    call_count = {"n": 0}

    def fake_eval(pref, *, repo, power_state=""):
        call_count["n"] += 1
        return IdleDecision(verdict="keep", reason="state_repo_unreachable")

    monkeypatch.setattr("api.routes.aks.autostop.evaluate_cluster", fake_eval)
    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        lambda *_a, **_kw: {"power_state": "Running", "healthy": True},
    )

    for _ in range(3):
        body = client.get(f"/api/aks/autostop/status?{_qs()}").json()
        assert body["reason"] == "state_repo_unreachable"
    # All three polls must re-compute (no caching of degraded results).
    assert call_count["n"] == 3


def test_status_singleflight_collapses_concurrent_polls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent cache-miss polls must collapse to one underlying
    compute. Without singleflight, 50 browsers polling the same cluster
    after a cache eviction all run the evaluator + ARM call in parallel."""
    import concurrent.futures
    import time as _time

    from api.services.auto_stop_evaluator import IdleDecision

    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 60,
        },
    )
    call_count = {"n": 0}

    def slow_eval(pref, *, repo, power_state=""):
        call_count["n"] += 1
        _time.sleep(0.2)  # Make the leader actually take time
        return IdleDecision(
            verdict="keep", reason="active", cluster_power_state=power_state
        )

    monkeypatch.setattr("api.routes.aks.autostop.evaluate_cluster", slow_eval)
    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        lambda *_a, **_kw: {"power_state": "Running", "healthy": True},
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _i: client.get(f"/api/aks/autostop/status?{_qs()}").status_code,
                range(8),
            )
        )
    assert all(code == 200 for code in results)
    # Exactly ONE compute despite 8 concurrent callers — followers wait
    # for the leader's cache fill instead of running in parallel.
    assert call_count["n"] == 1


class _FakeRedis:
    """Minimal in-memory Redis stub for autostop L2 cache tests
    (critique #18). Implements only the three methods the cache uses:
    ``get`` / ``setex`` / ``delete``.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> bytes | None:
        value = self.store.get(key)
        return value.encode("utf-8") if value is not None else None

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value

    def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self.store:
                self.store.pop(k)
                removed += 1
        return removed


def test_status_writes_to_l2_redis_for_cross_worker_sharing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critique #18: when a status compute succeeds, the body must be
    written to Redis (L2) so a sibling uvicorn worker serves the same
    cached body instead of re-running the entire Table+ARM+evaluator
    pipeline within the L2 TTL window.
    """
    fake = _FakeRedis()
    monkeypatch.setattr(
        "api.routes.aks.autostop._status_redis_client", lambda: fake
    )

    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 60,
        },
    )

    monkeypatch.setattr(
        "api.routes.aks.autostop.evaluate_cluster",
        lambda pref, *, repo, power_state="": IdleDecision(
            verdict="keep", reason="active", cluster_power_state=power_state
        ),
    )
    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        lambda *_a, **_kw: {"power_state": "Running", "healthy": True},
    )

    r1 = client.get(f"/api/aks/autostop/status?{_qs()}")
    assert r1.status_code == 200
    body1 = r1.json()
    expected_key = "autostop:status:sub-1|rg-elb|elb-cluster"
    assert expected_key in fake.store
    import json

    cached = json.loads(fake.store[expected_key])
    assert cached["verdict"] == body1["verdict"]
    assert cached["reason"] == body1["reason"]


def test_status_serves_l2_redis_hit_without_running_evaluator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critique #18: a sibling worker that already populated L2 must
    serve the cached body — the evaluator must NOT run again until
    the L2 entry expires or is invalidated.
    """
    fake = _FakeRedis()
    monkeypatch.setattr(
        "api.routes.aks.autostop._status_redis_client", lambda: fake
    )

    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 60,
        },
    )

    # Reset both tiers so the PUT-side write does not poison the assertion.
    from api.routes.aks.autostop import _reset_status_cache

    _reset_status_cache()
    fake.store.clear()

    import json

    seeded = {
        "exists": True,
        "editable": True,
        "owner_oid": "owner-oid",
        "enabled": True,
        "verdict": "warn",
        "reason": "idle:55m",
        "seconds_until_stop": 12,
        "next_stop_at": "",
        "active_job_count": 0,
        "idle_minutes": 60,
        "cooldown_minutes": 15,
    }
    fake.store["autostop:status:sub-1|rg-elb|elb-cluster"] = json.dumps(seeded)

    eval_calls = {"n": 0}

    def boom_eval(pref, *, repo, power_state=""):
        eval_calls["n"] += 1
        return IdleDecision(verdict="keep", reason="active")

    monkeypatch.setattr("api.routes.aks.autostop.evaluate_cluster", boom_eval)

    r = client.get(f"/api/aks/autostop/status?{_qs()}")
    assert r.status_code == 200
    body = r.json()
    # Must surface the L2 body verbatim instead of running the evaluator.
    assert body["verdict"] == "warn"
    assert body["reason"] == "idle:55m"
    assert eval_calls["n"] == 0


def test_put_drops_l2_redis_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critique #18: a PUT mutates the underlying preference, so it
    must delete the L2 cache entry too — otherwise a sibling worker
    keeps serving the pre-PUT body for up to ``_STATUS_L2_TTL_SECONDS``.
    """
    fake = _FakeRedis()
    monkeypatch.setattr(
        "api.routes.aks.autostop._status_redis_client", lambda: fake
    )

    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 60,
        },
    )

    monkeypatch.setattr(
        "api.routes.aks.autostop.evaluate_cluster",
        lambda pref, *, repo, power_state="": IdleDecision(
            verdict="keep", reason="active", cluster_power_state=power_state
        ),
    )
    monkeypatch.setattr(
        "api.services.cluster_health.get_cluster_health",
        lambda *_a, **_kw: {"power_state": "Running", "healthy": True},
    )

    client.get(f"/api/aks/autostop/status?{_qs()}")
    assert "autostop:status:sub-1|rg-elb|elb-cluster" in fake.store

    client.put(
        "/api/aks/autostop",
        json={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
            "enabled": True,
            "idle_minutes": 30,
        },
    )
    # The PUT both wrote the new pref and dropped the L2 entry.
    assert "autostop:status:sub-1|rg-elb|elb-cluster" not in fake.store
