"""Azure Blob data-plane helpers for BLAST storage workflows.

Responsibility: Azure Blob data-plane helpers for BLAST storage workflows
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `encode_blob_file_id`, `decode_blob_file_id`, `safe_download_filename`,
`result_media_type`, `upload_blob_bytes`, `upload_blob_text`, `container_usage_summaries`
Risky contracts: Validate Storage account/blob inputs and preserve the no-browser-SAS policy.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import threading
import zlib
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from typing import Any, cast

from azure.core.credentials import TokenCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

LOGGER = logging.getLogger(__name__)
_BLOB_FILE_ID_PREFIX = "b64_"


def encode_blob_file_id(blob_name: str) -> str:
    encoded = base64.urlsafe_b64encode(blob_name.encode("utf-8")).decode("ascii")
    return f"{_BLOB_FILE_ID_PREFIX}{encoded.rstrip('=')}"


def decode_blob_file_id(file_id: str) -> str | None:
    if not file_id.startswith(_BLOB_FILE_ID_PREFIX):
        return None
    value = file_id[len(_BLOB_FILE_ID_PREFIX) :]
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{value}{padding}").decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise ValueError("invalid file_id") from None
    if ".." in decoded or decoded.startswith("/") or "?" in decoded or "#" in decoded:
        raise ValueError("invalid file_id")
    return decoded


def safe_download_filename(blob_name: str) -> str:
    name = blob_name.rsplit("/", 1)[-1].strip() or "blast-result.out"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:128]
    return name or "blast-result.out"


def result_media_type(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".gz"):
        return "application/gzip"
    if lowered.endswith(".xml"):
        return "application/xml"
    if lowered.endswith((".out", ".log", ".txt")):
        return "text/plain"
    return "application/octet-stream"


def _blob_service(credential: TokenCredential, account_name: str) -> BlobServiceClient:
    # Validate the account name so a forged querystring can't redirect the
    # api sidecar's MI to an attacker-controlled URL. Azure storage account
    # names are 3-24 lowercase alphanumeric characters.
    if not _STORAGE_ACCOUNT_NAME_RE.fullmatch(account_name):
        raise ValueError(f"invalid storage account name: {account_name!r}")
    # Process-shared pool keyed by (id(credential), account_name). Reusing
    # the same BlobServiceClient lets azure-core keep its HTTP connection
    # pool warm across calls (saves the TLS handshake on every list/download)
    # and avoids re-running the credential discovery on first use. The
    # ``id(credential)`` half of the key means we never reuse a client built
    # against a different credential object (token leak guard); in normal
    # operation ``get_credential()`` is a process-wide singleton so the key
    # is effectively just the account name.
    pool_key = (id(credential), account_name)
    evicted_clients: list[BlobServiceClient] = []
    with _BLOB_SERVICE_POOL_LOCK:
        cached = _BLOB_SERVICE_POOL.get(pool_key)
        if cached is not None:
            _BLOB_SERVICE_POOL.move_to_end(pool_key)
            return cached
        # Fail fast on network-blocked accounts (publicNetworkAccess: Disabled)
        # instead of letting the SDK retry for ~30s. The api sidecar is the
        # only legitimate caller in production and reaches Storage via the
        # private endpoint, so failures here mean either RBAC deny or local
        # dev — both of which want a quick degraded response, not a long
        # retry storm.
        from api.services.storage_endpoint import blob_account_url

        client = BlobServiceClient(
            account_url=blob_account_url(account_name),
            credential=credential,
            retry_total=0,
            connection_timeout=5,
            read_timeout=10,
        )
        _BLOB_SERVICE_POOL[pool_key] = client
        while len(_BLOB_SERVICE_POOL) > _BLOB_SERVICE_POOL_MAX:
            _evicted_key, evicted = _BLOB_SERVICE_POOL.popitem(last=False)
            evicted_clients.append(evicted)
    # Close evicted clients outside the lock — BlobServiceClient.close()
    # tears down the underlying HTTP pool and may do synchronous IO; we
    # must not hold the pool lock while that happens or every other caller
    # blocks behind a slow shutdown.
    for evicted in evicted_clients:
        try:
            evicted.close()
        except Exception as exc:
            LOGGER.debug("blob service evict-close failed: %s", type(exc).__name__)
    return client


_BLOB_SERVICE_POOL_MAX = 32
_BLOB_SERVICE_POOL: OrderedDict[tuple[int, str], BlobServiceClient] = OrderedDict()
_BLOB_SERVICE_POOL_LOCK = threading.Lock()


def reset_blob_service_pool() -> None:
    """Drop every pooled BlobServiceClient.

    Test hook plus a safety valve for ``reset_credential()`` callers: holding
    on to a client built against a stale credential object would leak its
    token cache. Closing the clients releases the underlying HTTP pools.
    """
    with _BLOB_SERVICE_POOL_LOCK:
        clients = list(_BLOB_SERVICE_POOL.values())
        _BLOB_SERVICE_POOL.clear()
    for client in clients:
        try:
            client.close()
        except Exception as exc:
            LOGGER.debug("blob service reset-close failed: %s", type(exc).__name__)


_STORAGE_ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9]{3,24}$")


def _validate_blob_path(blob_path: str) -> None:
    if ".." in blob_path or blob_path.startswith("/") or "?" in blob_path or "#" in blob_path:
        raise ValueError("invalid blob_path: path traversal not allowed")


def upload_blob_bytes(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    data: bytes | Iterable[bytes],
    *,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload bytes to blob storage. Returns the blob URL."""
    _validate_blob_path(blob_path)
    svc = _blob_service(credential, account_name)
    blob = svc.get_blob_client(container, blob_path)
    blob.upload_blob(
        data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )
    return cast(str, blob.url)


def upload_blob_text(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    text: str,
    *,
    content_type: str = "text/plain; charset=utf-8",
) -> str:
    """Upload UTF-8 text to blob storage. Returns the blob URL."""
    return upload_blob_bytes(
        credential,
        account_name,
        container,
        blob_path,
        text.encode("utf-8"),
        content_type=content_type,
    )


def upload_query_text(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    fasta_text: str,
) -> str:
    """Upload FASTA text to blob storage. Returns the blob URL."""
    return upload_blob_text(credential, account_name, container, blob_path, fasta_text)


def upload_group_fasta(
    credential: TokenCredential,
    account_name: str,
    query_blob_path: str,
    group_fasta: str,
) -> str:
    """Upload a query-group FASTA payload to the queries container."""
    return upload_query_text(
        credential,
        account_name,
        "queries",
        query_blob_path,
        group_fasta,
    )


def read_blob_text(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    max_bytes: int = 4096,
) -> str:
    """Read the first max_bytes of a text blob. Returns UTF-8 text."""
    _validate_blob_path(blob_path)
    svc = _blob_service(credential, account_name)
    blob = svc.get_blob_client(container, blob_path)
    data = blob.download_blob(offset=0, length=max_bytes).readall()
    return data.decode("utf-8", errors="replace")


def read_result_blob_text(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
    max_bytes: int = 4096,
) -> str:
    """Read result text, transparently inflating gzip result blobs.

    BLAST results are often uploaded as `.out.gz`; reading those through
    `read_blob_text` returns compressed bytes, which makes XML/content sniffing
    impossible. This helper caps the decompressed payload so analytics routes
    remain bounded in the request thread.
    """
    if max_bytes <= 0:
        return ""
    if not blob_path.lower().endswith(".gz"):
        return read_blob_text(credential, account_name, container, blob_path, max_bytes=max_bytes)

    _validate_blob_path(blob_path)
    svc = _blob_service(credential, account_name)
    blob = svc.get_blob_client(container, blob_path)
    downloader = blob.download_blob()
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    chunks: list[bytes] = []
    total = 0
    for compressed in downloader.chunks():
        remaining = max_bytes - total
        if remaining <= 0:
            break
        data = inflater.decompress(compressed, remaining)
        if data:
            chunks.append(data)
            total += len(data)
    if total < max_bytes:
        flushed = inflater.flush(max_bytes - total)
        if flushed:
            chunks.append(flushed)
    return b"".join(chunks).decode("utf-8", errors="replace")


def stream_blob_bytes(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
) -> Iterator[bytes]:
    """Stream a blob through the api sidecar without issuing browser SAS."""
    _validate_blob_path(blob_path)
    svc = _blob_service(credential, account_name)
    blob = svc.get_blob_client(container, blob_path)
    downloader = blob.download_blob()
    yield from downloader.chunks()


def list_result_blobs(
    credential: TokenCredential,
    account_name: str,
    container: str,
    prefix: str,
) -> list[dict[str, Any]]:
    """List blobs under a results prefix."""
    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    blobs: list[dict[str, Any]] = []
    for blob in cc.list_blobs(name_starts_with=prefix):
        blobs.append(
            {
                "file_id": encode_blob_file_id(blob.name),
                "name": blob.name,
                "size": blob.size,
                "last_modified": blob.last_modified.isoformat() if blob.last_modified else None,
            }
        )
    return blobs


def container_usage_summaries(
    credential: TokenCredential,
    account_name: str,
    container_names: Iterable[str],
    *,
    max_blobs_per_container: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Return best-effort blob-count and byte-size totals for named containers."""
    svc = _blob_service(credential, account_name)
    summaries: dict[str, dict[str, Any]] = {}
    for container_name in container_names:
        total_size = 0
        blob_count = 0
        truncated = False
        try:
            cc = svc.get_container_client(container_name)
            for blob in cc.list_blobs():
                blob_count += 1
                size = getattr(blob, "size", None)
                if size is None:
                    size = getattr(blob, "content_length", None)
                if isinstance(size, int):
                    total_size += size
                if max_blobs_per_container is not None and blob_count >= max_blobs_per_container:
                    truncated = True
                    break
        except Exception as exc:
            summaries[container_name] = {
                "blob_count": None,
                "size_bytes": None,
                "usage_error": type(exc).__name__,
                "usage_truncated": False,
            }
            continue
        summaries[container_name] = {
            "blob_count": blob_count,
            "size_bytes": total_size,
            "usage_error": None,
            "usage_truncated": truncated,
        }
    return summaries


# NOTE: There is intentionally NO `generate_download_url` / SAS issuer here.
# Per .github/copilot-instructions.md §9, every Storage account stays
# `publicNetworkAccess: Disabled` and **the browser must never receive a SAS
# token**. Result downloads are served by streaming the blob through the api
# sidecar (1 MiB chunks, 4 MiB block uploads, semaphore-capped to 4 concurrent
# transfers). When that route is implemented, add a `stream_blob_to_response`
# helper here that returns an async iterator the FastAPI route can await — do
# NOT bring back `generate_blob_sas` / `get_user_delegation_key`.


def classify_storage_failure(
    credential: TokenCredential,
    subscription_id: str,
    resource_group: str,
    account_name: str,
    exc: BaseException,
) -> dict[str, Any]:
    """Classify a Storage data-plane exception into a UI-friendly degraded shape.

    Azure Storage returns the same ``AuthorizationFailure`` error code for two
    very different conditions:

    * **Network deny** — ``publicNetworkAccess: Disabled`` (or ``networkAcls``
      explicitly denies the caller) and the request did not arrive from a
      private endpoint. This is the steady-state for this project (see
      ``.github/copilot-instructions.md`` §9) and is **expected** when running
      the api sidecar from a developer laptop.
    * **RBAC deny** — the storage data plane is reachable but the caller lacks
      the ``Storage Blob Data *`` role at the account / container scope.

    To distinguish the two we look at the account's ``publicNetworkAccess``
    via ARM (management plane, which is reachable from anywhere with the right
    role). The result is the dict shape consumed by ``/api/blast/*`` routes.
    """
    err_str = str(exc)
    err_type = type(exc).__name__

    if (
        "ResourceNotFound" in err_str
        or "AccountNotFound" in err_str
        or "ContainerNotFound" in err_str
    ):
        suffix = f" in resource group '{resource_group}'." if resource_group else "."
        return {
            "degraded": True,
            "degraded_reason": "not_found",
            "message": f"Storage container or account '{account_name}' not found{suffix}",
        }

    is_permission_mismatch = "AuthorizationPermissionMismatch" in err_str
    is_authorization_failure = "AuthorizationFailure" in err_str
    is_auth_like = (
        is_authorization_failure
        or is_permission_mismatch
        or "This request is not authorized" in err_str
    )
    if not is_auth_like:
        return {
            "degraded": True,
            "degraded_reason": err_type,
            "message": f"Storage call failed: {err_type}",
        }

    public_state: str | None = None
    default_action: str | None = None
    ip_rules: list[str] = []
    if subscription_id and resource_group:
        try:
            from api.services.azure_clients import storage_client

            sc = storage_client(credential, subscription_id)
            account = sc.storage_accounts.get_properties(resource_group, account_name)
            raw = getattr(account, "public_network_access", None)
            public_state = str(raw) if raw is not None else None
            network_rule_set = getattr(account, "network_rule_set", None)
            if network_rule_set is not None:
                raw_default = getattr(network_rule_set, "default_action", None)
                default_action = str(raw_default) if raw_default is not None else None
                for rule in getattr(network_rule_set, "ip_rules", None) or []:
                    ip_value = getattr(rule, "ip_address_or_range", None) or getattr(
                        rule, "value", None
                    )
                    if ip_value:
                        ip_rules.append(str(ip_value))
        except Exception as arm_exc:
            LOGGER.debug("classify_storage_failure ARM check failed: %s", arm_exc)

    if public_state == "Disabled":
        return {
            "degraded": True,
            "degraded_reason": "network_blocked",
            "public_access_disabled": True,
            "message": (
                f"Storage account '{account_name}' is Private only "
                "(publicNetworkAccess: Disabled; production posture — see project policy §9). "
                "Data-plane access only works from inside the platform VNet via the "
                "private endpoint, so this view is unavailable from local development. "
                "To debug locally, open an "
                "IP-allowlisted window with "
                f"`scripts/dev/storage-public-access.sh on --account {account_name} "
                f"--rg {resource_group or '<resource-group>'}` and close it again with "
                "`storage-public-access.sh off` when done. In a deployed environment, "
                "run `azd up` and verify from the Container App."
            ),
        }

    if is_authorization_failure and public_state == "Enabled" and default_action == "Deny":
        caller_ip: str | None = None
        try:
            from api.services.storage_public_access import _detect_caller_ip

            caller_ip = _detect_caller_ip()
        except Exception as ip_exc:
            LOGGER.debug("classify_storage_failure caller IP check failed: %s", ip_exc)
        caller_ip_in_rules = bool(caller_ip and caller_ip in ip_rules)
        local_detail = (
            f" Current detected IP {caller_ip} is already in ipRules; Azure may still be "
            "propagating the firewall update or seeing a different egress path."
            if caller_ip_in_rules
            else f" Current detected IP {caller_ip or '<unknown>'} is not in ipRules."
        )
        return {
            "degraded": True,
            "degraded_reason": "firewall_blocked",
            "public_access_disabled": False,
            "local_debug_access_blocked": True,
            "caller_ip": caller_ip,
            "caller_ip_in_rules": caller_ip_in_rules,
            "message": (
                f"Storage account '{account_name}' allows only selected networks "
                "(publicNetworkAccess: Enabled, defaultAction: Deny), and Storage still "
                "rejected this local data-plane request. "
                f"Run `scripts/dev/storage-public-access.sh on --account {account_name} "
                f"--rg {resource_group or '<resource-group>'}` to refresh the IP allowlist, "
                "then retry after firewall propagation."
                f"{local_detail} If this persists after the network check passes, refresh "
                "your Azure CLI login so data-plane RBAC tokens are current."
            ),
        }

    if os.environ.get("CONTAINER_APP_NAME") or os.environ.get("IDENTITY_ENDPOINT"):
        remediation = (
            "Assign 'Storage Blob Data Reader' (or Contributor for write) on the "
            "storage account to the shared managed identity attached to this "
            "Container App, then wait a few minutes for RBAC propagation."
        )
    else:
        remediation = (
            "Assign 'Storage Blob Data Reader' (or Contributor for write) on the "
            "storage account to your az login identity, then wait a few minutes "
            "for RBAC propagation."
        )

    return {
        "degraded": True,
        "degraded_reason": "access_denied",
        "message": (
            f"Cannot read data from storage account '{account_name}'. "
            f"{remediation}"
        ),
    }


def list_databases(
    credential: TokenCredential,
    account_name: str,
    container: str = "blast-db",
) -> list[dict[str, Any]]:
    """List available BLAST databases in the blast-db container.

    BLAST databases consist of multiple files like core_nt.00.nhd,
    core_nt.00.nhi, core_nt.nal, etc. We extract the base DB name
    by stripping the volume number and extension suffixes.
    """
    # Known BLAST DB file extensions
    _DB_EXTS = {
        ".nhd",
        ".nhi",
        ".nhr",
        ".nin",
        ".nnd",
        ".nni",
        ".nog",
        ".nsq",
        ".nxm",
        ".nal",
        ".ndb",
        ".njs",
        ".nos",
        ".not",
        ".ntf",
        ".nto",
        ".phd",
        ".phi",
        ".phr",
        ".pin",
        ".pnd",
        ".pni",
        ".pog",
        ".psq",
        ".pxm",
        ".pal",
        ".pdb",
        ".pjs",
        ".pos",
        ".pot",
        ".ptf",
        ".pto",
    }
    svc = _blob_service(credential, account_name)
    cc = svc.get_container_client(container)
    db_info: dict[str, dict[str, Any]] = {}
    metadata_blobs: dict[str, str] = {}  # db_name -> metadata json content
    oracle_status_blobs: dict[str, str] = {}  # db_name -> oracle status json content
    oracle_part_counts: dict[str, int] = {}
    blastdb_json_blobs: dict[str, str] = {}  # db_name -> BLAST v5 .njs content
    for blob in cc.list_blobs():
        parts = blob.name.split("/")
        name = parts[-1]  # file name without directory prefix
        # Detect the prefix to distinguish NCBI (top-level) from custom (custom_db/)
        is_custom = len(parts) >= 3 and parts[0] == "custom_db"
        # Collect metadata files separately
        if name.endswith("-metadata.json"):
            meta_db_name = name.replace("-metadata.json", "")
            try:
                bc = cc.get_blob_client(blob.name)
                metadata_blobs[meta_db_name] = bc.download_blob().readall().decode("utf-8")
            except Exception as exc:
                LOGGER.debug("metadata blob read skipped for %s: %s", blob.name, exc)
            continue
        if (
            len(parts) == 4
            and parts[0] == "metadata"
            and parts[1] == "oracles"
            and parts[3] == "status.json"
        ):
            try:
                bc = cc.get_blob_client(blob.name)
                oracle_status_blobs[parts[2]] = bc.download_blob().readall().decode("utf-8")
            except Exception as exc:
                LOGGER.debug("oracle status blob read skipped for %s: %s", blob.name, exc)
            continue
        if (
            len(parts) == 6
            and parts[0] == "metadata"
            and parts[1] == "oracles"
            and parts[3] == "parts"
        ):
            oracle_part_counts[parts[2]] = oracle_part_counts.get(parts[2], 0) + 1
            continue
        if name.endswith(".njs"):
            base = re.sub(r"\.\d+$", "", name[:-4])
            try:
                bc = cc.get_blob_client(blob.name)
                blastdb_json_blobs[base] = bc.download_blob().readall().decode("utf-8")
            except Exception as exc:
                LOGGER.debug("BLAST DB metadata read skipped for %s: %s", blob.name, exc)
        # Skip staging artifacts
        if parts[0] in ("custom-db-build",) or (len(parts) >= 2 and parts[1] == ".staging"):
            continue
        # Skip prepare-db shard layout artifacts. ensure_shard_sets() writes
        # files under `{N}shards/{db}_shard_{i:02d}/...` (manifest + .nal).
        # Without this guard, the .nal at e.g.
        # `1shards/16S_ribosomal_RNA_shard_00/16S_ribosomal_RNA_shard_00.nal`
        # would be parsed as a brand-new "DB" called
        # `16S_ribosomal_RNA_shard_00`, polluting the dashboard.
        if re.match(r"^\d+shards$", parts[0]):
            continue
        # Check if file has a known BLAST extension
        for ext in _DB_EXTS:
            if name.endswith(ext):
                base = name[: -len(ext)]
                # Strip volume number suffix (e.g. ".00", ".01")
                base = re.sub(r"\.\d+$", "", base)
                if base:
                    if base not in db_info:
                        # Build the blob prefix so the frontend can reconstruct the full path
                        prefix = f"custom_db/{base}" if is_custom else base
                        db_info[base] = {
                            "name": base,
                            "container": container,
                            "prefix": prefix,
                            "source": "custom" if is_custom else "ncbi",
                            "file_count": 0,
                            "total_bytes": 0,
                            "last_modified": None,
                        }
                    db_info[base]["file_count"] += 1
                    db_info[base]["total_bytes"] += blob.size or 0
                    blob_modified = blob.last_modified
                    if blob_modified:
                        mod_str = (
                            blob_modified.isoformat()
                            if hasattr(blob_modified, "isoformat")
                            else str(blob_modified)
                        )
                        prev = db_info[base]["last_modified"]
                        if not prev or mod_str > prev:
                            db_info[base]["last_modified"] = mod_str
                break
    # Enrich with metadata (source_version, downloaded_at, sharding info)
    import json as _json

    from api.services.web_blast_searchsp import WEB_BLAST_SEARCHSP_DEFAULTS

    for db_name, info in db_info.items():
        # Default sharding fields so the frontend can rely on their presence.
        info.setdefault("sharded", False)
        info.setdefault("shard_sets", [])
        info.setdefault("shard_source_version", None)
        info.setdefault("shards_stale", False)
        info.setdefault("sharding_in_progress", False)
        info.setdefault("sharding_started_at", None)
        info.setdefault("sharding_error", None)
        info.setdefault("update_in_progress", False)
        info.setdefault("updating_to_source_version", None)
        info.setdefault("update_started_at", None)
        info.setdefault("update_completed_at", None)
        info.setdefault("update_error", None)
        info.setdefault("update_failed_at", None)
        if db_name in blastdb_json_blobs:
            try:
                blast_meta = _json.loads(blastdb_json_blobs[db_name])
                for source, target in (
                    ("number-of-letters", "total_letters"),
                    ("number-of-sequences", "total_sequences"),
                    ("bytes-to-cache", "bytes_to_cache"),
                    ("bytes-total", "bytes_total"),
                ):
                    value = blast_meta.get(source)
                    if isinstance(value, (int, float)) and value > 0:
                        info[target] = int(value)
                for source, target in (
                    ("title", "title"),
                    ("description", "description"),
                    ("dbtype", "molecule_type"),
                    ("last-updated", "update_date"),
                    ("last_updated", "update_date"),
                    ("date", "update_date"),
                ):
                    value = blast_meta.get(source)
                    if isinstance(value, str) and value.strip():
                        info[target] = value.strip()
            except Exception as exc:
                LOGGER.debug("BLAST DB .njs metadata parse skipped for %s: %s", db_name, exc)
        if db_name in metadata_blobs:
            try:
                meta = _json.loads(metadata_blobs[db_name])
                info["source_version"] = meta.get("source_version")
                info["downloaded_at"] = meta.get("downloaded_at")
                # Sharding metadata written by the prepare-db pipeline once
                # the per-DB shard set upload completes. Both keys are
                # optional — older metadata blobs (pre-2026-05) won't have
                # them, in which case the defaults above hold.
                if isinstance(meta.get("sharded"), bool):
                    info["sharded"] = meta["sharded"]
                shard_sets = meta.get("shard_sets")
                if isinstance(shard_sets, list):
                    # Coerce to a sorted list of unique ints for a stable
                    # contract with the SPA.
                    info["shard_sets"] = sorted(
                        {
                            int(n)
                            for n in shard_sets
                            if isinstance(n, (int, str)) and str(n).isdigit()
                        }
                    )
                shard_source_version = meta.get("shard_source_version")
                if isinstance(shard_source_version, str) and shard_source_version.strip():
                    info["shard_source_version"] = shard_source_version.strip()
                elif info.get("sharded") and info.get("source_version"):
                    # Legacy metadata predates explicit shard generation tagging; treat
                    # the existing layouts as belonging to the recorded DB generation.
                    info["shard_source_version"] = info.get("source_version")
                db_source_version = str(info.get("source_version") or "")
                shard_version = str(info.get("shard_source_version") or "")
                info["shards_stale"] = bool(
                    info.get("sharded") and db_source_version and shard_version != db_source_version
                )
                # In-flight shard state surfaced from the daemon-thread
                # writer in /api/blast/databases/{db}/shard. The SPA
                # renders these directly so a page reload still shows
                # "sharding…" while a background thread is running.
                if isinstance(meta.get("sharding_in_progress"), bool):
                    info["sharding_in_progress"] = meta["sharding_in_progress"]
                if isinstance(meta.get("sharding_started_at"), str):
                    info["sharding_started_at"] = meta["sharding_started_at"]
                if isinstance(meta.get("sharding_error"), str):
                    info["sharding_error"] = meta["sharding_error"][:300]
                if isinstance(meta.get("update_in_progress"), bool):
                    info["update_in_progress"] = meta["update_in_progress"]
                for key in (
                    "updating_to_source_version",
                    "update_started_at",
                    "update_completed_at",
                    "update_failed_at",
                ):
                    if isinstance(meta.get(key), str):
                        info[key] = meta[key]
                if isinstance(meta.get("update_error"), str):
                    info["update_error"] = meta["update_error"][:300]
                # Hardened prepare-db pipeline fields. ``copy_status`` is the
                # authoritative replacement for the SPA's old "90% of files
                # arrived = Ready" heuristic — when phase == "completed" the
                # download truly succeeded; "partial" / "init_failed" /
                # "copying" are honest in-flight or partial states.
                if isinstance(meta.get("copy_status"), dict):
                    info["copy_status"] = meta["copy_status"]
                if isinstance(meta.get("failed_files"), list):
                    info["failed_files"] = [
                        item
                        for item in meta["failed_files"]
                        if isinstance(item, dict)
                    ][:50]
                # ETag of a stable NCBI key (the .tar.gz.md5 we picked when
                # the DB was prepared). The SPA uses it for per-DB update
                # detection that does NOT fire whenever NCBI rotates
                # latest-dir.
                if isinstance(meta.get("signature_etag"), str):
                    info["signature_etag"] = meta["signature_etag"]
                # Allow metadata to override total_bytes if the prepare-db
                # pipeline computed it more precisely than blob enumeration
                # (e.g. for very large multi-volume DBs).
                if isinstance(meta.get("total_bytes"), (int, float)) and meta["total_bytes"] > 0:
                    info["total_bytes"] = int(meta["total_bytes"])
                for key in ("total_letters", "total_sequences", "bytes_to_cache", "bytes_total"):
                    if isinstance(meta.get(key), (int, float)) and meta[key] > 0:
                        info[key] = int(meta[key])
                for source_key in ("effective_search_space", "db_effective_search_space"):
                    if isinstance(meta.get(source_key), (int, float)) and meta[source_key] > 0:
                        info["db_effective_search_space"] = int(meta[source_key])
                        info["db_effective_search_space_source"] = "storage_metadata"
                        break
            except Exception as exc:
                LOGGER.debug("metadata blob parse skipped for %s: %s", db_name, exc)
        if db_name in oracle_status_blobs:
            try:
                oracle = _json.loads(oracle_status_blobs[db_name])
                if isinstance(oracle, dict):
                    expected_parts = int(oracle.get("expected_parts") or 0)
                    ready_parts = int(oracle_part_counts.get(db_name, 0))
                    db_source_version = str(info.get("source_version") or "")
                    oracle_source_version = str(oracle.get("source_version") or "")
                    source_version_stale = bool(
                        db_source_version and oracle_source_version != db_source_version
                    )
                    info["db_order_oracle"] = {
                        "status": (
                            "stale"
                            if source_version_stale
                            else "ready"
                            if expected_parts > 0 and ready_parts >= expected_parts
                            else str(oracle.get("status") or "building")
                        ),
                        "run_id": oracle.get("run_id"),
                        "started_at": oracle.get("started_at"),
                        "source_version": oracle.get("source_version"),
                        "expected_parts": expected_parts,
                        "ready_parts": ready_parts,
                        "part_prefix": oracle.get("part_prefix"),
                    }
            except Exception as exc:
                LOGGER.debug("oracle status blob parse skipped for %s: %s", db_name, exc)
        default_searchsp = WEB_BLAST_SEARCHSP_DEFAULTS.get(db_name)
        if default_searchsp is not None:
            info.setdefault("web_blast_searchsp", default_searchsp.value)
            info.setdefault("web_blast_searchsp_scope", default_searchsp.scope)
            info.setdefault("web_blast_searchsp_evidence", default_searchsp.evidence)
    return sorted(db_info.values(), key=lambda d: d["name"])
