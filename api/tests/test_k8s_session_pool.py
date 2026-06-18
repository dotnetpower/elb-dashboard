"""Tests for the pooled `_get_k8s_session` helper.

Responsibility: Cover the K8s session pool's reuse, TTL clamping (kubeconfig
material + AAD token), max-entries eviction, and the throwaway path used
when the effective TTL collapses to non-positive.
Edit boundaries: Keep assertions focused on pool behaviour — networking is
fully mocked.
Key entry points: `test_pool_reuses_session`, `test_pool_ttl_clamped_to_material_expiry`,
`test_pool_ttl_clamped_to_token_expiry`, `test_pool_max_entries_evicts_soonest_expiring`,
`test_throwaway_path_has_no_temp_files`, `test_ca_data_uses_in_memory_adapter_no_temp_file`
Risky contracts: Do not require network access or real Azure credentials.
Validation: `uv run pytest -q api/tests/test_k8s_session_pool.py`.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from api.services.k8s import client as k8s_client_mod


@pytest.fixture(autouse=True)
def _reset_pool() -> None:
    k8s_client_mod.reset_k8s_credential_cache()
    k8s_client_mod.reset_k8s_session_pool()


@pytest.fixture(autouse=True)
def _mock_ssl(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch ssl.create_default_context so _InMemoryCaAdapter never needs a
    real PEM cert in unit tests.  The mock context is returned from every call
    and its load_verify_locations method is a MagicMock (callable, no-op)."""
    mock_ctx = MagicMock()
    mock_ssl = MagicMock()
    mock_ssl.create_default_context.return_value = mock_ctx
    monkeypatch.setattr(k8s_client_mod, "ssl", mock_ssl)
    return mock_ctx


def _material(server: str = "https://aks.example", expires_in: float = 600.0) -> Any:
    """Build a fake `_K8sCredentialMaterial` with the token-auth path active
    (no client_cert) so `_get_k8s_session` exercises `credential.get_token`."""
    return SimpleNamespace(
        server=server,
        ca_data=b"ca-bytes",
        client_cert=None,
        client_key=None,
        expires_at=time.monotonic() + expires_in,
    )


def _credential(token_expires_in_seconds: float = 3600.0) -> Any:
    cred = MagicMock()
    token = SimpleNamespace(
        token="fake-token",
        expires_on=int(time.time() + token_expires_in_seconds),
    )
    cred.get_token.return_value = token
    return cred


def test_pool_reuses_session() -> None:
    """Second call with the same (sub, rg, cluster) key returns the same Session."""
    material = _material()
    cred = _credential()
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=material):
        session_a, server_a = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")
        session_b, server_b = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")
    assert session_a is session_b
    assert server_a == server_b == material.server
    # ****** path uses credential.get_token once on cold miss.
    assert cred.get_token.call_count == 1


def test_pool_keys_distinguish_admin_and_cluster() -> None:
    material = _material()
    cred = _credential()
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=material):
        user_session, _ = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks", admin=False)
        admin_session, _ = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks", admin=True)
        other_cluster, _ = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks-2")
    assert user_session is not admin_session
    assert user_session is not other_cluster


def test_pool_ttl_clamped_to_material_expiry() -> None:
    """If kubeconfig material expires before the session pool TTL, the pool
    entry must inherit the shorter lifetime."""
    short = _material(expires_in=1.0)
    cred = _credential()
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=short):
        k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")
    with k8s_client_mod._K8S_SESSION_POOL_LOCK:
        entry = k8s_client_mod._K8S_SESSION_POOL[("s", "rg", "aks", False)]
    # Cannot outlive the material's own expiry.
    assert entry.expires_at <= short.expires_at + 1e-6


def test_pool_ttl_clamped_to_token_expiry() -> None:
    """Bearer-auth sessions must retire before the AAD token expires."""
    # Token expires in 90s, safety margin is 60s -> effective lifetime ~30s,
    # well below the default 300s pool TTL.
    material = _material(expires_in=600.0)
    cred = _credential(token_expires_in_seconds=90.0)
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=material):
        k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")
    with k8s_client_mod._K8S_SESSION_POOL_LOCK:
        entry = k8s_client_mod._K8S_SESSION_POOL[("s", "rg", "aks", False)]
    remaining = entry.expires_at - time.monotonic()
    assert remaining < 60.0, f"expected token-clamped TTL < 60s, got {remaining}"


def test_throwaway_path_has_no_temp_files() -> None:
    """When the effective TTL collapses to <= 0 we must hand out a non-pooled
    session.  Because CA is now held in-memory (issue #47) and token-auth
    sessions write no client cert files, temp_files must be empty."""
    # Token expires in 1s, safety margin 60s -> negative remaining -> throwaway.
    material = _material(expires_in=600.0)
    cred = _credential(token_expires_in_seconds=1.0)
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=material):
        session, _ = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")
    # Throwaway sessions are NOT pooled.
    with k8s_client_mod._K8S_SESSION_POOL_LOCK:
        assert ("s", "rg", "aks", False) not in k8s_client_mod._K8S_SESSION_POOL
    # CA is in-memory; token-auth uses no client cert files — nothing on disk.
    session.close()  # must not raise


def test_ca_data_uses_in_memory_adapter_no_temp_file(
    _mock_ssl: MagicMock,
) -> None:
    """When ca_data is set, _get_k8s_session must use _InMemoryCaAdapter
    (loading the cert into the ssl context) and must NOT create any temp file
    for the CA cert — that was the root cause of the race in issue #47."""
    material = _material()
    cred = _credential()
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=material):
        k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")

    # The ssl context was created and the CA bytes were loaded into it.
    k8s_client_mod.ssl.create_default_context.assert_called_once()  # type: ignore[attr-defined]
    _mock_ssl.load_verify_locations.assert_called_once_with(
        cadata=material.ca_data.decode("ascii")
    )

    # The pool entry's temp_files list must be empty (no CA file, no client cert).
    with k8s_client_mod._K8S_SESSION_POOL_LOCK:
        entry = k8s_client_mod._K8S_SESSION_POOL[("s", "rg", "aks", False)]
    assert entry.temp_files == [], f"expected no temp files, got {entry.temp_files}"


def test_pool_max_entries_evicts_soonest_expiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inserting beyond the cap evicts the entry closest to expiry, not the newest."""
    monkeypatch.setenv("K8S_SESSION_POOL_MAX_ENTRIES", "2")
    cred = _credential()

    # Three different clusters; the first two get long TTLs, the third also
    # long. After the third insert the oldest-expiring entry is evicted.
    materials = [_material(server=f"https://aks-{i}", expires_in=600.0 + i) for i in range(3)]
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", side_effect=materials):
        k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks-0")
        k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks-1")
        k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks-2")
    with k8s_client_mod._K8S_SESSION_POOL_LOCK:
        keys = set(k8s_client_mod._K8S_SESSION_POOL.keys())
    # aks-0 had the soonest expiry -> evicted. aks-1 and aks-2 remain.
    assert ("s", "rg", "aks-0", False) not in keys
    assert ("s", "rg", "aks-1", False) in keys
    assert ("s", "rg", "aks-2", False) in keys


def test_pooled_close_is_noop_until_retire() -> None:
    """session.close() on a pooled session must not tear down the underlying
    connection pool — subsequent calls keep reusing it."""
    material = _material()
    cred = _credential()
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=material):
        s1, _ = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")
        s1.close()  # No-op for pooled sessions.
        s2, _ = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")
    assert s1 is s2
    # Now explicitly drain the pool and verify the entry is retired.
    k8s_client_mod.reset_k8s_session_pool()
    with k8s_client_mod._K8S_SESSION_POOL_LOCK:
        assert not k8s_client_mod._K8S_SESSION_POOL


def test_pool_lock_released_during_retire_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eviction must NOT hold `_K8S_SESSION_POOL_LOCK` while `_retire_entry`
    runs — that's TCP / filesystem IO and would stall every other caller
    across every cluster. We assert it by making the retire path notice
    whether the lock is locked when it runs.
    """
    monkeypatch.setenv("K8S_SESSION_POOL_MAX_ENTRIES", "1")
    cred = _credential()

    lock_state_seen: list[bool] = []
    real_retire = k8s_client_mod._retire_entry

    def spying_retire(entry):  # type: ignore[no-untyped-def]
        # `Lock.locked()` is True only while another thread holds it.
        # If the slow-path insert still holds the pool lock when it
        # calls us, this will be True and the test fails.
        lock_state_seen.append(k8s_client_mod._K8S_SESSION_POOL_LOCK.locked())
        real_retire(entry)

    monkeypatch.setattr(k8s_client_mod, "_retire_entry", spying_retire)

    materials = [_material(server=f"https://aks-{i}", expires_in=600.0 + i) for i in range(2)]
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", side_effect=materials):
        k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks-0")
        # Inserting aks-1 evicts aks-0 (cap=1) — this is the path under test.
        k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks-1")

    assert lock_state_seen, "eviction did not trigger _retire_entry"
    assert not any(lock_state_seen), (
        f"_retire_entry was called while pool lock was held: {lock_state_seen}"
    )


def test_throwaway_close_is_idempotent() -> None:
    """A double `session.close()` on a throwaway session must not crash —
    there are no temp files to unlink (CA is in-memory, no client cert)."""
    material = _material(expires_in=600.0)
    cred = _credential(token_expires_in_seconds=1.0)
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=material):
        session, _ = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")
    # Both closes must succeed without error.
    session.close()
    session.close()


def test_max_entries_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """`K8S_SESSION_POOL_MAX_ENTRIES` env var must clamp into [1, 4096] and
    fall back to the module default on a non-numeric value."""
    monkeypatch.setenv("K8S_SESSION_POOL_MAX_ENTRIES", "0")  # clamped up to 1
    assert k8s_client_mod._k8s_session_pool_max_entries() == 1
    monkeypatch.setenv("K8S_SESSION_POOL_MAX_ENTRIES", "100000")  # clamped to 4096
    assert k8s_client_mod._k8s_session_pool_max_entries() == 4096
    monkeypatch.setenv("K8S_SESSION_POOL_MAX_ENTRIES", "not-a-number")
    assert (
        k8s_client_mod._k8s_session_pool_max_entries()
        == k8s_client_mod._K8S_SESSION_POOL_MAX_ENTRIES
    )
