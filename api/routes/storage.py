"""Storage routes (`/api/storage/*`).

Currently exposes the BLAST database "prepare" flow: a server-side copy
from NCBI's public S3 bucket (`ncbi-blast-databases.s3.amazonaws.com`) into
the workload Storage account's `blast-db` container, using
`start_copy_from_url` so the bytes never traverse the api sidecar.

Architecture notes
------------------
* The api sidecar reaches the workload Storage account over the platform
  VNet's private endpoint, so the account stays
  ``publicNetworkAccess: Disabled`` at all times — there is **no**
  temporary public-window toggle (cf. ``.github/copilot-instructions.md``
  §9). The legacy Function App had to flip the flag because it ran outside
  the VNet; the Container App api sidecar does not.
* ``start_copy_from_url`` is a server-side copy — Azure Storage itself
  fetches each NCBI S3 URL out to the public internet, which is
  independent of the storage account's *inbound* public network setting.
* The HTTP handler returns immediately with the source-blob count; per-file
  ``start_copy_from_url`` calls and the metadata write run in a background
  ``ThreadPoolExecutor(20)`` so large databases (e.g. ``core_nt``,
  ~600 files) don't blow the ingress timeout.
* The SPA polls ``GET /api/blast/databases`` to observe progress; this
  route is fire-and-forget on purpose.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from threading import Thread
from typing import Any
from xml.etree import ElementTree

from fastapi import APIRouter, Body, Depends, HTTPException

from api.auth import CallerIdentity, require_caller
from api.services import get_credential
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/storage", tags=["storage"])

# NCBI's public S3 bucket holding the latest BLAST databases. Public-read,
# unauthenticated GET. The "latest-dir" object holds the timestamp prefix
# (e.g. "2024-12-09-01-05-02") that points to the freshest snapshot.
_NCBI_S3_BASE = "https://ncbi-blast-databases.s3.amazonaws.com"
_S3_LIST_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

# Validation patterns — kept narrow on purpose. NCBI database names are
# `[A-Za-z0-9_]+` (e.g. `16S_ribosomal_RNA`, `core_nt`), storage account
# names are `[a-z0-9]{3,24}`, resource groups follow the ARM rules below.
_RE_DB_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RE_STORAGE_ACCOUNT = re.compile(r"^[a-z0-9]{3,24}$")
_RE_RG = re.compile(r"^[-\w._()]{1,90}$")
_RE_SUB = re.compile(r"^[0-9a-fA-F-]{36}$")


def _check(value: str, pattern: re.Pattern[str], label: str) -> None:
    if not isinstance(value, str) or not pattern.match(value):
        raise HTTPException(400, f"invalid {label}: '{sanitise(str(value)[:40])}'")


def _resolve_latest_dir() -> str:
    """Return the latest snapshot directory name from NCBI."""
    import httpx

    resp = httpx.get(f"{_NCBI_S3_BASE}/latest-dir", timeout=15.0)
    resp.raise_for_status()
    return resp.text.strip()


def _list_keys(latest_dir: str, db_name: str) -> list[str]:
    """List the S3 keys for ``{latest_dir}/{db_name}*``.

    NCBI publishes BLAST DBs as multiple sharded files plus a few small
    metadata files; for large DBs (``core_nt``, ``nr``) this is hundreds
    of objects spread across paginated XML responses.
    """
    import httpx

    prefix = f"{latest_dir}/{db_name}"
    keys: list[str] = []
    continuation = ""
    # Hard cap at 50 pages x 1000 objects = 50k keys to bound surprise.
    with httpx.Client(timeout=30.0) as client:
        for _page in range(50):
            list_url = f"{_NCBI_S3_BASE}?list-type=2&prefix={prefix}&max-keys=1000"
            if continuation:
                list_url += f"&continuation-token={continuation}"
            resp = client.get(list_url)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)  # noqa: S314 — NCBI public bucket, schema fixed
            for el in root.findall(".//s3:Contents/s3:Key", _S3_LIST_NS):
                if el.text and not el.text.endswith("/"):
                    keys.append(el.text)
            is_truncated = root.findtext("s3:IsTruncated", "false", _S3_LIST_NS)
            if is_truncated == "true":
                tok = root.find("s3:NextContinuationToken", _S3_LIST_NS)
                continuation = tok.text if tok is not None and tok.text else ""
            else:
                break
    return keys


@router.post("/prepare-db")
def prepare_db(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Begin a server-side copy of a BLAST DB from NCBI to the workload
    Storage account's ``blast-db`` container.

    Returns immediately. Per-file ``start_copy_from_url`` calls run in a
    daemon thread; the SPA observes progress by polling
    ``GET /api/blast/databases``.
    """
    sub = body.get("subscription_id", "")
    storage_rg = body.get("storage_resource_group", "")
    account_name = body.get("account_name", "")
    db_name = body.get("db_name", "")
    if not all([sub, storage_rg, account_name, db_name]):
        raise HTTPException(
            400,
            "subscription_id, storage_resource_group, account_name, db_name required",
        )
    _check(sub, _RE_SUB, "subscription_id")
    _check(storage_rg, _RE_RG, "storage_resource_group")
    _check(account_name, _RE_STORAGE_ACCOUNT, "account_name")
    _check(db_name, _RE_DB_NAME, "db_name")

    cred = get_credential()

    # Local-debug only: when LOCAL_DEBUG_AUTO_OPEN_STORAGE=true is set on a
    # developer laptop (NOT in a Container App), open the workload Storage
    # account's public network surface to this caller's IP so the server-side
    # copy below can actually reach the data plane. In production the api
    # sidecar already reaches Storage via the private endpoint and this is a
    # no-op. See api/services/storage_public_access.py and project policy §9.
    from api.services.storage_public_access import ensure_local_storage_access

    access = ensure_local_storage_access(cred, sub, storage_rg, account_name)
    if access.get("action") == "failed":
        LOGGER.warning(
            "prepare_db: local-debug auto-open failed for %s: %s",
            account_name,
            access.get("error"),
        )

    try:
        latest_dir = _resolve_latest_dir()
    except Exception as exc:
        LOGGER.warning("NCBI latest-dir lookup failed: %s", type(exc).__name__)
        raise HTTPException(502, f"could not contact NCBI: {sanitise(str(exc))[:200]}") from exc

    try:
        all_keys = _list_keys(latest_dir, db_name)
    except Exception as exc:
        LOGGER.warning("NCBI key list failed for %s: %s", db_name, type(exc).__name__)
        raise HTTPException(
            502, f"could not list NCBI database keys: {sanitise(str(exc))[:200]}"
        ) from exc

    if not all_keys:
        raise HTTPException(
            404,
            f"No files found for database '{db_name}' in NCBI S3 (dir: {latest_dir})",
        )

    # Build the destination container client. The api sidecar reaches the
    # storage account over the private endpoint via the shared MI; no SAS
    # is involved, no public network toggle is performed.
    from azure.storage.blob import BlobServiceClient

    blob_svc = BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=cred,
    )
    container = blob_svc.get_container_client("blast-db")

    def _do_copies() -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _copy_one(key: str) -> tuple[str, str]:
            source_url = f"{_NCBI_S3_BASE}/{key}"
            # Layout MUST match `elastic-blast` upstream
            # `util.py:get_blastdb_info`: it calls `os.path.dirname(db_url)`
            # and runs `azcopy list`, then filters lines containing
            # `os.path.basename(db)`. That requires files to live in a
            # subfolder named after the DB (`blast-db/<db>/<files>`). A
            # flat layout makes `azcopy list` of the parent return wrong
            # results and elastic-blast reports
            # "BLAST database … was not found".
            file_basename = key.split("/")[-1]
            blob_name = f"{db_name}/{file_basename}"
            try:
                container.get_blob_client(blob_name).start_copy_from_url(source_url)
                return (blob_name, "started")
            except Exception as e:
                if "PendingCopyOperation" in str(e):
                    return (blob_name, "skipped")
                LOGGER.warning("Copy failed for %s: %s", blob_name, sanitise(str(e))[:200])
                return (blob_name, "error")

        started = skipped = errors = 0
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = [ex.submit(_copy_one, k) for k in all_keys]
            for f in as_completed(futures):
                _, status = f.result()
                if status == "started":
                    started += 1
                elif status == "skipped":
                    skipped += 1
                else:
                    errors += 1

        LOGGER.info(
            "DB prepare done for %s: %d started, %d skipped, %d errors",
            db_name,
            started,
            skipped,
            errors,
        )

        # Auto-shard step: as soon as the NCBI key enumeration is in hand,
        # we know every volume name and can publish the manifest+.nal alias
        # files for every preset shard count. The blobs they reference do
        # not have to exist yet — the AKS init script will download them
        # lazily at job runtime. ~50 KB total per DB across all presets.
        from api.services.db_sharding import (
            PRESET_SHARD_SETS,
            derive_volumes_from_keys,
            upload_shard_set,
        )

        shard_sets_created: list[int] = []
        try:
            volumes = derive_volumes_from_keys(db_name, all_keys)
            for n in PRESET_SHARD_SETS:
                if n > len(volumes):
                    continue  # small DB, fewer volumes than this preset
                try:
                    upload_shard_set(
                        cred,
                        account_name,
                        db_name,
                        n,
                        volumes,
                    )
                    shard_sets_created.append(n)
                except Exception as exc:
                    LOGGER.warning(
                        "shard set N=%d failed for %s: %s",
                        n,
                        db_name,
                        sanitise(str(exc))[:200],
                    )
        except LookupError:
            # No volumes detected (e.g. key list was empty or unfamiliar
            # extension layout). Leave sharded=False.
            LOGGER.info("auto-shard skipped for %s: no volumes detected", db_name)
        except Exception as exc:
            LOGGER.warning(
                "auto-shard failed for %s: %s",
                db_name,
                sanitise(str(exc))[:200],
            )

        # Drop a metadata blob alongside the DB so the dashboard can show
        # source_version / downloaded_at / sharding state without
        # contacting NCBI again.
        try:
            metadata_blob = container.get_blob_client(f"{db_name}-metadata.json")
            metadata_blob.upload_blob(
                json.dumps(
                    {
                        "db_name": db_name,
                        "source_version": latest_dir,
                        "downloaded_at": datetime.now(UTC).isoformat(),
                        "file_count": started + skipped,
                        "sharded": bool(shard_sets_created),
                        "shard_sets": shard_sets_created,
                    }
                ).encode("utf-8"),
                overwrite=True,
            )
        except Exception as e:
            LOGGER.warning("metadata write failed for %s: %s", db_name, sanitise(str(e))[:200])

    Thread(target=_do_copies, daemon=True, name=f"prepare-db-{db_name}").start()

    LOGGER.info(
        "prepare_db started oid=%s db=%s files=%d source=%s access=%s",
        caller.object_id,
        db_name,
        len(all_keys),
        latest_dir,
        access.get("action"),
    )
    response: dict[str, Any] = {
        "ok": True,
        "db_name": db_name,
        # Async — actual progress is observed by polling /api/blast/databases.
        "files_copied": 0,
        "files_total": len(all_keys),
        "source_version": latest_dir,
        "output": (
            f"Started background copy of {len(all_keys)} files from {latest_dir}. "
            "Poll /api/blast/databases for progress."
        ),
        "async": True,
    }
    if access.get("action") in ("opened", "ip_added"):
        response["local_debug_storage_opened"] = {
            "ip": access.get("ip"),
            "previous_public": access.get("previous_public"),
            "off_hint": access.get("off_hint"),
        }
        response["output"] += (
            f" Local-debug: temporarily opened Storage to {access.get('ip')} "
            f"(was publicNetworkAccess={access.get('previous_public')}). Run "
            f"`{access.get('off_hint')}` when done."
        )
    return response


# ---------------------------------------------------------------------------
# Local-debug helpers — surface the Storage public-access toggle in the UI
# but only when the api process is NOT running inside a Container App. The
# Container-App guard lives in api.services.storage_public_access and is the
# load-bearing safety check; do not bypass it.
# ---------------------------------------------------------------------------
@router.get("/local-debug")
def storage_local_debug_status(
    subscription_id: str = "",
    resource_group: str = "",
    account_name: str = "",
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Return whether the dashboard should expose the local-debug toggle.

    Always returns 200. ``is_local`` is the only field the SPA needs to gate
    the button visibility. ``public_access``, ``default_action``, and
    ``ip_rules`` are best-effort context for the confirmation modal.
    """
    from api.services.storage_public_access import (
        is_running_locally,
        read_local_storage_state,
    )

    if not is_running_locally():
        # Deployed: the toggle button must never appear in the UI.
        return {"is_local": False}

    if not (subscription_id and resource_group and account_name):
        # Local but no account scope yet — still tell the SPA we are local so
        # it can render the affordance once the user picks an RG / account.
        return {
            "is_local": True,
            "public_access": None,
            "default_action": None,
            "ip_rules": [],
            "caller_ip": None,
            "caller_ip_in_rules": False,
        }

    try:
        _check(subscription_id, _RE_SUB, "subscription_id")
        _check(resource_group, _RE_RG, "resource_group")
        _check(account_name, _RE_STORAGE_ACCOUNT, "account_name")
    except HTTPException:
        return {"is_local": True, "error": "invalid scope parameters"}

    cred = get_credential()
    return read_local_storage_state(cred, subscription_id, resource_group, account_name)


@router.post("/local-debug/open")
def storage_local_debug_open(
    body: dict[str, Any] = Body(...),
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Open the workload Storage account's public network surface to the
    caller's IP. Local-only (refuses inside a Container App).

    The request is the explicit operator confirmation, so the env-var gate
    (``LOCAL_DEBUG_AUTO_OPEN_STORAGE``) is bypassed. The Container-App guard
    is NOT bypassed — see ``ensure_local_storage_access(force=True)``.
    """
    from api.services.storage_public_access import (
        ensure_local_storage_access,
        is_running_locally,
    )

    if not is_running_locally():
        raise HTTPException(
            status_code=403,
            detail="storage local-debug toggle is unavailable in deployed environments",
        )

    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    account_name = body.get("account_name", "")
    if not all([sub, rg, account_name]):
        raise HTTPException(400, "subscription_id, resource_group, account_name required")
    _check(sub, _RE_SUB, "subscription_id")
    _check(rg, _RE_RG, "resource_group")
    _check(account_name, _RE_STORAGE_ACCOUNT, "account_name")

    cred = get_credential()
    result = ensure_local_storage_access(cred, sub, rg, account_name, force=True)
    LOGGER.info(
        "storage_local_debug_open oid=%s account=%s action=%s",
        caller.object_id,
        account_name,
        result.get("action"),
    )
    if result.get("action") == "failed":
        raise HTTPException(500, f"could not open storage: {result.get('error')}")
    return result
