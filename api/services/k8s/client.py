"""Kubernetes API session and AKS kubeconfig credential helpers."""

from __future__ import annotations

import base64
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any

import yaml  # type: ignore[import-untyped]
from azure.core.credentials import TokenCredential

from api.services.azure_clients import aks_client

_AKS_SERVER_APP_ID = "6dae42f8-4368-4678-94ff-3960e28e3630"
_K8S_CREDENTIAL_CACHE_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class _K8sCredentialMaterial:
    server: str
    ca_data: bytes | None
    client_cert: bytes | None
    client_key: bytes | None
    expires_at: float


_K8S_CREDENTIAL_CACHE: dict[tuple[str, str, str, bool], _K8sCredentialMaterial] = {}
_K8S_CREDENTIAL_CACHE_LOCK = threading.Lock()


def reset_k8s_credential_cache() -> None:
    """Clear cached AKS kubeconfig material. Test-only."""
    with _K8S_CREDENTIAL_CACHE_LOCK:
        _K8S_CREDENTIAL_CACHE.clear()


def _k8s_credential_cache_ttl() -> float:
    raw = os.environ.get("K8S_CREDENTIAL_CACHE_TTL_SECONDS", "")
    if raw:
        try:
            return max(0.0, min(float(raw), 3600.0))
        except ValueError:
            return _K8S_CREDENTIAL_CACHE_TTL_SECONDS
    return _K8S_CREDENTIAL_CACHE_TTL_SECONDS


def _get_k8s_credential_material(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool,
) -> _K8sCredentialMaterial:
    cache_key = (subscription_id, resource_group, cluster_name, admin)
    now = time.monotonic()
    with _K8S_CREDENTIAL_CACHE_LOCK:
        cached = _K8S_CREDENTIAL_CACHE.get(cache_key)
    if cached is not None and cached.expires_at > now:
        return cached

    client = aks_client(credential, subscription_id)
    if admin:
        creds = client.managed_clusters.list_cluster_admin_credentials(
            resource_group,
            cluster_name,
        )
    else:
        creds = client.managed_clusters.list_cluster_user_credentials(
            resource_group,
            cluster_name,
        )
    kubeconfig_bytes = creds.kubeconfigs[0].value
    kubeconfig = yaml.safe_load(bytes(kubeconfig_bytes))

    cluster_info = kubeconfig["clusters"][0]["cluster"]
    user_info = kubeconfig["users"][0]["user"]
    ca_data = cluster_info.get("certificate-authority-data", "")
    client_cert = user_info.get("client-certificate-data")
    client_key = user_info.get("client-key-data")

    material = _K8sCredentialMaterial(
        server=cluster_info["server"],
        ca_data=base64.b64decode(ca_data) if ca_data else None,
        client_cert=base64.b64decode(client_cert) if client_cert else None,
        client_key=base64.b64decode(client_key) if client_key else None,
        expires_at=now + _k8s_credential_cache_ttl(),
    )
    if material.expires_at > now:
        with _K8S_CREDENTIAL_CACHE_LOCK:
            _K8S_CREDENTIAL_CACHE[cache_key] = material
    return material


def _get_k8s_session(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    cluster_name: str,
    *,
    admin: bool = False,
) -> tuple[Any, str]:
    """Return ``(requests.Session, server_url)`` for direct K8s API calls.

    The session owns any temporary CA/client-cert files and deletes them when
    ``session.close()`` is called. Temp files are also cleaned up on partial
    setup failure so credential material never lingers after an exception.
    """

    import requests as _requests

    material = _get_k8s_credential_material(
        credential,
        subscription_id,
        resource_group,
        cluster_name,
        admin=admin,
    )

    session = _requests.Session()
    temp_files: list[str] = []

    def cleanup_temp_files() -> None:
        for path in temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass

    def write_secret_file(suffix: str, content: bytes) -> str:
        handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            handle.write(content)
            handle.flush()
        finally:
            handle.close()
        os.chmod(handle.name, 0o600)
        temp_files.append(handle.name)
        return handle.name

    try:
        if material.ca_data:
            session.verify = write_secret_file(".crt", material.ca_data)
        else:
            session.verify = True

        if material.client_cert and material.client_key:
            cert_path = write_secret_file(".crt", material.client_cert)
            key_path = write_secret_file(".key", material.client_key)
            session.cert = (cert_path, key_path)
        else:
            token = credential.get_token(f"{_AKS_SERVER_APP_ID}/.default")
            session.headers["Authorization"] = f"Bearer {token.token}"
    except Exception:
        cleanup_temp_files()
        try:
            session.close()
        except Exception:  # noqa: S110 - session close failures are non-actionable here
            pass
        raise

    original_close = session.close

    def cleanup_close() -> None:
        try:
            original_close()
        finally:
            cleanup_temp_files()

    session.close = cleanup_close  # type: ignore[assignment]
    return session, material.server
