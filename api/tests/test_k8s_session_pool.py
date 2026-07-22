"""Tests for the pooled `_get_k8s_session` helper.

Responsibility: Cover the K8s session pool's reuse, TTL clamping (kubeconfig
material + AAD token), max-entries eviction, and the throwaway path used
when the effective TTL collapses to non-positive.
Edit boundaries: Keep assertions focused on pool behaviour — networking is
fully mocked.
Key entry points: `test_pool_reuses_session`, `test_pool_ttl_clamped_to_material_expiry`,
`test_pool_ttl_clamped_to_token_expiry`, `test_pool_max_entries_evicts_soonest_expiring`,
`test_throwaway_path_leaves_no_client_cert_on_disk`,
`test_ca_in_memory_survives_pool_eviction_during_inflight_get`,
`test_client_cert_in_memory_survives_pool_eviction`
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


def _make_test_ca_pem() -> bytes:
    """A real self-signed CA PEM so the in-memory SSLContext built by
    `_build_k8s_https_adapter` (issue #47) can actually load it. `ssl`
    rejects non-PEM bytes, so the old `b"ca-bytes"` placeholder no longer
    works now that the CA is parsed instead of just written to a file."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "elb-test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(Encoding.PEM)


def _make_test_client_cert() -> tuple[bytes, bytes]:
    """A real self-signed client cert + unencrypted private key PEM pair.

    `ssl.SSLContext.load_cert_chain` (used by the in-memory client-cert path)
    parses these, so the old `b"client-cert-bytes"` placeholder no longer
    works now that the mTLS material is loaded into an SSLContext instead of
    written to `session.cert`."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "elb-test-client")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(Encoding.PEM)
    key_pem = key.private_bytes(
        Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
    )
    return cert_pem, key_pem


_TEST_CA_PEM = _make_test_ca_pem()
_TEST_CLIENT_CERT_PEM, _TEST_CLIENT_KEY_PEM = _make_test_client_cert()


@pytest.fixture(autouse=True)
def _reset_pool() -> None:
    k8s_client_mod.reset_k8s_credential_cache()
    k8s_client_mod.reset_k8s_session_pool()


def _material(
    server: str = "https://aks.example",
    expires_in: float = 600.0,
    *,
    client_cert: bytes | None = None,
    client_key: bytes | None = None,
) -> Any:
    """Build a fake `_K8sCredentialMaterial`.

    Default (no client_cert) exercises the Bearer token path so
    `_get_k8s_session` calls `credential.get_token`. Passing client_cert /
    client_key exercises the admin mTLS path, which now loads the cert / key
    into an in-memory SSLContext (issue #47 extended to the client cert) —
    no credential material lands on disk in either path."""
    return SimpleNamespace(
        server=server,
        ca_data=_TEST_CA_PEM,
        client_cert=client_cert,
        client_key=client_key,
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
    # Bearer token path uses credential.get_token once on cold miss.
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


def test_throwaway_path_leaves_no_client_cert_on_disk() -> None:
    """When the effective TTL collapses to <= 0 we hand out a non-pooled
    session. The admin mTLS client cert / key are now parsed into an in-memory
    SSLContext (issue #47 extended to the client cert), so NO cert file is left
    on disk and `session.cert` is never set — the race that produced
    `Could not find the TLS certificate file, invalid path: /tmp/elb-k8s-*.crt`
    is gone."""
    import ssl

    # Material already expired -> entry_expires_at <= now -> throwaway path.
    material = _material(
        expires_in=-1.0,
        client_cert=_TEST_CLIENT_CERT_PEM,
        client_key=_TEST_CLIENT_KEY_PEM,
    )
    cred = _credential()
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=material):
        session, _ = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")
    # Throwaway sessions are NOT pooled.
    with k8s_client_mod._K8S_SESSION_POOL_LOCK:
        assert ("s", "rg", "aks", False) not in k8s_client_mod._K8S_SESSION_POOL
    # mTLS is handled by the in-memory context; no cert path is exposed and
    # verify stays True.
    assert not session.cert
    assert session.verify is True
    https_adapter = session.get_adapter("https://aks.example")
    ctx = https_adapter.poolmanager.connection_pool_kw.get("ssl_context")
    assert isinstance(ctx, ssl.SSLContext)
    # close() is a clean teardown with nothing on disk to unlink.
    session.close()
    assert not session.cert


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
    """A double `session.close()` on a throwaway session must not crash — with
    the client cert now in-memory there are no temp files to unlink, so both
    closes are clean teardowns."""
    material = _material(
        expires_in=-1.0,
        client_cert=_TEST_CLIENT_CERT_PEM,
        client_key=_TEST_CLIENT_KEY_PEM,
    )
    cred = _credential()
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=material):
        session, _ = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")
    # No client cert files on disk; both closes are clean teardowns.
    session.close()
    session.close()
    assert not session.cert


def test_ca_in_memory_survives_pool_eviction_during_inflight_get() -> None:
    """Issue #47: the CA must be in-memory so pool eviction cannot delete a
    bundle a borrowed session still references.

    The Bearer path writes NO temp file at all (CA -> SSLContext), so eviction
    has nothing to unlink out from under an in-flight GET. We verify the
    invariant and then drive a GET on the borrowed session AFTER the pool has
    been drained — it must succeed with ``verify=True`` (never a deleted path).
    """
    import ssl

    import requests

    material = _material()  # Bearer path, ca_data = real PEM, no client cert.
    cred = _credential()
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=material):
        session, _ = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks")

    with k8s_client_mod._K8S_SESSION_POOL_LOCK:
        entry = k8s_client_mod._K8S_SESSION_POOL[("s", "rg", "aks", False)]
    # No CA bundle on disk -> eviction cannot delete a borrowed CA file.
    assert entry.temp_files == []
    assert session.verify is True
    # The https adapter carries the cluster CA in an in-memory SSLContext.
    https_adapter = session.get_adapter("https://aks.example")
    ctx = https_adapter.poolmanager.connection_pool_kw.get("ssl_context")
    assert isinstance(ctx, ssl.SSLContext)

    # A borrower still holds `session`. Drain the pool (this is the eviction /
    # atexit path that previously unlinked the CA temp file).
    borrowed = session
    k8s_client_mod.reset_k8s_session_pool()

    # In-flight GET after eviction: stub the transport so the test is
    # deterministic, and assert `verify` reaching the adapter is the truthy
    # in-memory marker, never a now-deleted filesystem path (the old OSError).
    captured: dict[str, Any] = {}

    def fake_send(self: Any, request: Any, **kwargs: Any) -> Any:
        captured["verify"] = kwargs.get("verify")
        response = requests.models.Response()
        response.status_code = 200
        response.url = request.url
        return response

    with patch.object(requests.adapters.HTTPAdapter, "send", fake_send):
        result = borrowed.get("https://aks.example/healthz", timeout=1)

    assert result.status_code == 200
    assert captured["verify"] is True


def test_client_cert_in_memory_survives_pool_eviction() -> None:
    """Regression for the sharded-BLAST warm-up incident: the mTLS client cert
    / key must be in-memory so pool eviction cannot delete a bundle a borrowed
    admin session still references.

    Before the fix, ``_get_k8s_session`` wrote the client cert / key to
    ``/tmp/elb-k8s-*.crt|.key`` and set ``session.cert = (cert, key)``. Pooled
    sessions outlive a request, so ``reset_k8s_session_pool`` / atexit unlinked
    those files while a warm-readiness poll still held the session — the next
    GET raised ``Could not find the TLS certificate file, invalid path:
    /tmp/elb-k8s-*.crt``, which gated the Service Bus drain indefinitely. The
    client cert now lands in an in-memory SSLContext, so eviction has nothing
    to unlink.
    """
    import ssl

    import requests

    material = _material(
        client_cert=_TEST_CLIENT_CERT_PEM,
        client_key=_TEST_CLIENT_KEY_PEM,
    )
    cred = _credential()
    with patch.object(k8s_client_mod, "_get_k8s_credential_material", return_value=material):
        session, _ = k8s_client_mod._get_k8s_session(cred, "s", "rg", "aks", admin=True)

    with k8s_client_mod._K8S_SESSION_POOL_LOCK:
        entry = k8s_client_mod._K8S_SESSION_POOL[("s", "rg", "aks", True)]
    # No credential material on disk -> eviction cannot delete a borrowed cert.
    assert entry.temp_files == []
    assert not session.cert
    assert session.verify is True
    # The client cert lives in the adapter's in-memory SSLContext, not a file.
    https_adapter = session.get_adapter("https://aks.example")
    ctx = https_adapter.poolmanager.connection_pool_kw.get("ssl_context")
    assert isinstance(ctx, ssl.SSLContext)
    # The admin path must NOT fall back to a Bearer token.
    assert "Authorization" not in session.headers
    assert cred.get_token.call_count == 0

    # A borrower still holds `session`. Drain the pool (eviction / atexit path
    # that previously unlinked the client cert temp files).
    borrowed = session
    k8s_client_mod.reset_k8s_session_pool()

    captured: dict[str, Any] = {}

    def fake_send(self: Any, request: Any, **kwargs: Any) -> Any:
        captured["cert"] = kwargs.get("cert")
        captured["verify"] = kwargs.get("verify")
        response = requests.models.Response()
        response.status_code = 200
        response.url = request.url
        return response

    with patch.object(requests.adapters.HTTPAdapter, "send", fake_send):
        result = borrowed.get("https://aks.example/healthz", timeout=1)

    assert result.status_code == 200
    # Never a now-deleted filesystem cert path — mTLS is in the SSLContext.
    assert not captured["cert"]
    assert captured["verify"] is True


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
