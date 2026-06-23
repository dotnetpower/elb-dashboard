"""Blob upload, read, stream, and listing helpers.

Responsibility: Storage blob I/O operations that use the shared BlobServiceClient
pool, with bounded streaming and metadata/result read caps.
Edit boundaries: Keep direct blob upload/download/listing here. ID encoding,
path validation, pool lifecycle, usage summaries, and failure classification live
in sibling modules.
Key entry points: upload/read/stream/list-result helpers.
Risky contracts: Browser downloads must stream through the API sidecar; never
issue SAS URLs or browser-fetchable Storage URLs.
Validation: `uv run pytest -q api/tests/test_storage_data.py`.
"""

from __future__ import annotations

import logging
import os
import threading
import zlib
from collections.abc import Iterable, Iterator
from typing import Any, cast

from azure.core.credentials import TokenCredential
from azure.core.exceptions import HttpResponseError
from azure.storage.blob import ContentSettings

from api.services.storage.blob_ids import encode_blob_file_id
from api.services.storage.blob_paths import _validate_blob_path
from api.services.storage.client_pool import _blob_service

LOGGER = logging.getLogger(__name__)


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
    """Read the first max_bytes of a text blob. Returns UTF-8 text.

    A 0-byte blob (e.g. BLAST FAILURE.txt that exists but is empty,
    metadata stub from a partial upload) makes Azure Storage return
    HTTP 416 InvalidRange for any explicit byte range — surface it as
    an empty string so callers don't have to special-case 416 separately
    from the more common ``ResourceNotFoundError``.
    """
    _validate_blob_path(blob_path)
    svc = _blob_service(credential, account_name)
    blob = svc.get_blob_client(container, blob_path)
    try:
        data = blob.download_blob(offset=0, length=max_bytes).readall()
    except HttpResponseError as exc:
        status = getattr(exc, "status_code", None)
        error_code = getattr(exc, "error_code", None)
        if status == 416 or error_code == "InvalidRange":
            return ""
        raise
    return data.decode("utf-8", errors="replace")


# Cap for control-plane metadata blob reads (workload Storage JSON, oracle
# status, BLAST v5 ``.njs``). Catches a corrupted or maliciously oversized
# blob before it OOMs the api/worker sidecar. NCBI ``.njs`` files we have
# observed top out near 1 MiB; the 16 MiB ceiling leaves headroom while
# still bounding worst-case memory per call. Callers that already know
# their blob is tiny (e.g. ``*-metadata.json``) pass a smaller ``max_bytes``.
METADATA_BLOB_MAX_BYTES = 16 * 1024 * 1024


def read_metadata_blob_bytes(
    blob_client: Any,
    *,
    max_bytes: int = METADATA_BLOB_MAX_BYTES,
    label: str = "metadata",
) -> bytes:
    """Read a small metadata blob with a hard size cap.

    Uses the SDK's ``download_blob(length=max_bytes + 1)`` so the over-cap
    detection runs server-side — we never pull more than ``max_bytes + 1``
    bytes across the network. Raises ``ValueError`` with the size in the
    message when the blob exceeds the cap so callers can log + degrade
    cleanly instead of OOM'ing the worker.

    A 0-byte blob (e.g. a freshly-created append blob with no events yet)
    makes Azure Storage return ``HTTP 416 InvalidRange`` for any
    explicit byte range — collapse that to an empty ``b""`` so callers
    don't have to special-case the empty-but-existing condition.
    """
    try:
        raw = blob_client.download_blob(offset=0, length=max_bytes + 1).readall()
    except HttpResponseError as exc:
        status = getattr(exc, "status_code", None)
        error_code = getattr(exc, "error_code", None)
        if status == 416 or error_code == "InvalidRange":
            return b""
        raise
    if len(raw) > max_bytes:
        LOGGER.warning(
            "%s blob exceeds %d bytes (got %d); refusing to load",
            label,
            max_bytes,
            len(raw),
        )
        raise ValueError(
            f"{label} blob exceeds {max_bytes} bytes (got {len(raw)}); refusing to load"
        )
    return raw


def read_metadata_blob_text(
    blob_client: Any,
    *,
    max_bytes: int = METADATA_BLOB_MAX_BYTES,
    label: str = "metadata",
) -> str:
    """UTF-8 text wrapper around :func:`read_metadata_blob_bytes`."""
    return read_metadata_blob_bytes(blob_client, max_bytes=max_bytes, label=label).decode(
        "utf-8"
    )


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
        # NB: ``Decompress.flush(length)`` treats ``length`` as the INITIAL
        # output-buffer size, not a hard cap — it returns ALL remaining
        # decompressed output (including anything stashed in
        # ``unconsumed_tail`` by the bounded ``decompress`` calls above), which
        # can be far larger than the ``max_bytes`` budget. Slice to the
        # remaining budget so the documented ``max_bytes`` contract holds and a
        # highly-compressible blob cannot blow the request thread's memory.
        flushed = inflater.flush()
        if flushed:
            chunks.append(flushed[: max_bytes - total])
    return b"".join(chunks).decode("utf-8", errors="replace")


def stream_blob_bytes(
    credential: TokenCredential,
    account_name: str,
    container: str,
    blob_path: str,
) -> Iterator[bytes]:
    """Stream a blob through the api sidecar without issuing browser SAS.

    Wraps every active download in a bounded semaphore (default 8 permits,
    env ``STORAGE_STREAM_MAX_CONCURRENCY``). Without this cap, ten
    simultaneous browser tab opens could each pin one BlobServiceClient
    HTTP connection AND the api/worker thread serving them, starving every
    other in-flight Storage call. The permit is held for the entire
    generator lifetime so the upstream gating matches the actual resource
    usage (one TCP socket per concurrent download).
    """
    _validate_blob_path(blob_path)
    svc = _blob_service(credential, account_name)
    blob = svc.get_blob_client(container, blob_path)
    if not _STREAM_DOWNLOAD_SEMAPHORE.acquire(timeout=_STREAM_DOWNLOAD_ACQUIRE_TIMEOUT_SECONDS):
        raise RuntimeError(
            "storage download semaphore exhausted: too many concurrent transfers"
        )
    try:
        downloader = blob.download_blob()
        yield from downloader.chunks()
    finally:
        _STREAM_DOWNLOAD_SEMAPHORE.release()


_STREAM_DOWNLOAD_MAX_CONCURRENCY = max(
    1, int(os.environ.get("STORAGE_STREAM_MAX_CONCURRENCY", "8"))
)
_STREAM_DOWNLOAD_ACQUIRE_TIMEOUT_SECONDS = float(
    os.environ.get("STORAGE_STREAM_ACQUIRE_TIMEOUT_SECONDS", "60")
)
_STREAM_DOWNLOAD_SEMAPHORE = threading.BoundedSemaphore(_STREAM_DOWNLOAD_MAX_CONCURRENCY)


def list_result_blobs(
    credential: TokenCredential,
    account_name: str,
    container: str,
    prefix: str,
    *,
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    """List blobs under a results prefix."""
    limit = max_results
    if limit is None:
        limit = max(1, int(os.environ.get("STORAGE_RESULT_BLOB_LIST_LIMIT", "5000")))
    # When the ADLS Gen2 (dfs) data-plane is enabled, list via the native
    # directory walk (get_paths) for true hierarchical enumeration. On any dfs
    # error fall back to the Blob prefix scan so a transient dfs issue never
    # breaks result listing. Both target the same HNS account and return the
    # same row shape.
    from api.services.storage.dfs_client_pool import dfs_enabled

    if dfs_enabled():
        try:
            from api.services.storage.dfs_io import list_paths_dfs

            return list_paths_dfs(credential, account_name, container, prefix, limit=limit)
        except Exception as exc:
            LOGGER.warning(
                "dfs listing failed (prefix=%s), falling back to blob: %s",
                prefix,
                type(exc).__name__,
            )
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
        if len(blobs) >= limit:
            break
    return blobs
