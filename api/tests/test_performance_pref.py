"""Tests for the per-cluster Performance preference service.

Responsibility: Verify the warm-cache-mode preference round-trips through the
    file backend, enforces the closed enum, and defaults to ``ephemeral``.
Edit boundaries: Service-level behaviour only; route shaping is covered by
    ``test_settings_performance.py``.
Key entry points: ``test_save_get_round_trip``, ``test_invalid_mode_defaults``,
    ``test_resolve_defaults_when_missing``.
Risky contracts: The default mode must stay ``ephemeral`` so a missing row keeps
    the historical provisioning behaviour.
Validation: ``uv run pytest -q api/tests/test_performance_pref.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _file_backend(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the file backend: no CONTAINER_APP_NAME, isolated state dir.
    monkeypatch.delenv("CONTAINER_APP_NAME", raising=False)
    monkeypatch.delenv("AZURE_TABLE_ENDPOINT", raising=False)
    monkeypatch.setenv("ELB_LOCAL_STATE_DIR", str(tmp_path))


def test_save_get_round_trip() -> None:
    from api.services.performance_pref import (
        PerformancePreference,
        get_performance_preference,
        save_performance_preference,
    )

    pref = PerformancePreference(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="aks-elb",
        warm_cache_mode="node_disk",
    )
    save_performance_preference(pref)

    loaded = get_performance_preference("sub-1", "rg-elb", "aks-elb")
    assert loaded is not None
    assert loaded.warm_cache_mode == "node_disk"
    assert loaded.cluster_name == "aks-elb"


def test_missing_row_returns_none() -> None:
    from api.services.performance_pref import get_performance_preference

    assert get_performance_preference("sub-x", "rg-x", "aks-x") is None


def test_resolve_defaults_when_missing() -> None:
    from api.services.performance_pref import (
        DEFAULT_WARM_CACHE_MODE,
        resolve_warm_cache_mode,
    )

    assert DEFAULT_WARM_CACHE_MODE == "ephemeral"
    assert resolve_warm_cache_mode("sub-x", "rg-x", "aks-x") == "ephemeral"


def test_invalid_mode_normalises_to_default() -> None:
    from api.services.performance_pref import PerformancePreference

    pref = PerformancePreference.from_dict(
        {
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "aks-elb",
            "warm_cache_mode": "bogus",
        }
    )
    assert pref.warm_cache_mode == "ephemeral"


def test_normalise_requires_identity_fields() -> None:
    from api.services.performance_pref import normalise_preference

    with pytest.raises(ValueError, match="subscription_id"):
        normalise_preference({"resource_group": "rg", "cluster_name": "c"})
    with pytest.raises(ValueError, match="resource_group"):
        normalise_preference({"subscription_id": "s", "cluster_name": "c"})
    with pytest.raises(ValueError, match="cluster_name"):
        normalise_preference({"subscription_id": "s", "resource_group": "rg"})


def test_resolve_reads_saved_mode() -> None:
    from api.services.performance_pref import (
        PerformancePreference,
        resolve_warm_cache_mode,
        save_performance_preference,
    )

    save_performance_preference(
        PerformancePreference(
            subscription_id="sub-2",
            resource_group="rg-elb",
            cluster_name="aks-2",
            warm_cache_mode="data_disk",
        )
    )
    assert resolve_warm_cache_mode("sub-2", "rg-elb", "aks-2") == "data_disk"


def test_list_returns_saved_rows() -> None:
    from api.services.performance_pref import (
        PerformancePreference,
        list_performance_preferences,
        save_performance_preference,
    )

    save_performance_preference(
        PerformancePreference("s1", "rg", "c1", warm_cache_mode="node_disk")
    )
    save_performance_preference(
        PerformancePreference("s2", "rg", "c2", warm_cache_mode="data_disk")
    )
    rows = list_performance_preferences()
    modes = {r.cluster_name: r.warm_cache_mode for r in rows}
    assert modes["c1"] == "node_disk"
    assert modes["c2"] == "data_disk"
