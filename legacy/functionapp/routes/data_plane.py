"""ACR image build + BLAST DB prepare (long-running, async).

Two routes that kick off long ACR Build / blob-copy work and return
immediately. Status is polled via the existing monitor/* endpoints
and `blast/databases` listing respectively.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import azure.durable_functions as df
import azure.functions as func
import requests as _requests

from _http_utils import (
    _RE_ACR_NAME,
    _RE_DB_NAME,
    _RE_STORAGE_ACCOUNT,
    _error_response,
    _json_response,
    _validate_name,
    _validate_rg,
    _validate_sub,
)
from auth.token import AuthError, validate_bearer_token
from services.azure_clients import credential_for_caller
from services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

bp = df.Blueprint()


@bp.route(route="acr/build-images", methods=["POST"])
def build_acr_images(req: func.HttpRequest) -> func.HttpResponse:
    """Build ElasticBLAST images in ACR via ACR Build Tasks.

    Schedules builds and returns immediately with run IDs. The UI polls
    monitor/acr to track build status — no HTTP thread blocking.
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    body = json.loads(raw.decode("utf-8"))
    sub = body.get("subscription_id", "")
    rg = body.get("resource_group", "")
    registry = body.get("registry_name", "")
    if not all([sub, rg, registry]):
        return _error_response(400, "subscription_id, resource_group, registry_name required")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    if err := _validate_rg(rg):
        return _error_response(400, err)
    if err := _validate_name(registry, _RE_ACR_NAME, "registry_name"):
        return _error_response(400, err)

    cred = credential_for_caller(identity.raw_token)

    from services.image_tags import IMAGE_TAGS, IMAGE_BUILD_INFO, SOURCE_REPO, SOURCE_BRANCH
    from azure.mgmt.containerregistry import ContainerRegistryManagementClient

    requested_images = body.get("images", [])  # empty = all
    from azure.mgmt.containerregistry.models import (
        DockerBuildRequest,
        EncodedTaskRunRequest,
        OS,
        PlatformProperties,
    )

    acr = ContainerRegistryManagementClient(cred, sub, api_version="2019-06-01-preview")
    results = []
    pollers: list[tuple[str, Any]] = []
    for image, tag in IMAGE_TAGS.items():
        if requested_images and image not in requested_images:
            continue
        full_image = f"{image}:{tag}"
        build_info = IMAGE_BUILD_INFO.get(image, {})
        context_path = build_info.get("context", "")
        dockerfile = build_info.get("dockerfile", "Dockerfile")
        if context_path:
            source_location = f"{SOURCE_REPO}#{SOURCE_BRANCH}:{context_path}"
        else:
            source_location = f"{SOURCE_REPO}#{SOURCE_BRANCH}"
        LOGGER.info("Scheduling ACR build for %s (source: %s, dockerfile: %s)", full_image, source_location, dockerfile)
        try:
            pre_build = build_info.get("pre_build_cmd", "")
            if pre_build:
                # Build context for the docker step — defaults to "." (the
                # uploaded source root). When build_context_dir is set the
                # build step descends into that subdirectory so Dockerfile
                # COPY directives resolve against subdir-local files.
                build_context_dir = build_info.get("build_context_dir", ".")
                task_yaml = f"""version: v1.1.0
steps:
  - cmd: bash -c "{pre_build}"
  - build: -f {dockerfile} -t $Registry/{full_image} {build_context_dir}
  - push:
    - $Registry/{full_image}
"""
                build_req: Any = EncodedTaskRunRequest(
                    encoded_task_content=base64.b64encode(task_yaml.encode()).decode(),
                    source_location=f"{SOURCE_REPO}#{SOURCE_BRANCH}",
                    platform=PlatformProperties(os=OS.LINUX),
                    timeout=3600,
                )
            else:
                build_req = DockerBuildRequest(
                    docker_file_path=dockerfile,
                    image_names=[full_image],
                    source_location=source_location,
                    is_push_enabled=True,
                    platform=PlatformProperties(os=OS.LINUX),
                    timeout=3600,
                )
            poller = acr.registries.begin_schedule_run(rg, registry, build_req)
            pollers.append((full_image, poller))
        except Exception as exc:
            LOGGER.warning("ACR build schedule failed for %s: %s", full_image, exc)
            results.append({"image": full_image, "status": "failed", "error": sanitise(str(exc))})

    for full_image, poller in pollers:
        try:
            run_result = poller.result()
            run_id = run_result.run_id or ""
            status = run_result.status or "Queued"
            results.append({"image": full_image, "status": "scheduled", "run_id": run_id, "acr_status": status})
        except Exception as exc:
            LOGGER.warning("ACR build schedule failed for %s: %s", full_image, exc)
            results.append({"image": full_image, "status": "failed", "error": sanitise(str(exc))})
    return _json_response({"results": results})


@bp.route(route="storage/prepare-db", methods=["POST"])
def prepare_blast_db(req: func.HttpRequest) -> func.HttpResponse:
    """Download BLAST database from NCBI to Azure Blob Storage.

    Server-side copy via Azure Blob `start_copy_from_url` directly from NCBI's
    public S3 bucket. No VM, no azcopy, no az login required.

    The HTTP handler returns immediately with the source-blob count; per-file
    `start_copy_from_url` calls and the metadata write run in a background
    `ThreadPoolExecutor(20)` so large databases (e.g. core_nt, ~600 files)
    don't blow the SWA proxy timeout.
    """
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)
    raw = req.get_body()
    if not raw:
        return _error_response(400, "request body required")
    body = json.loads(raw.decode("utf-8"))
    sub = body.get("subscription_id", "")
    storage_rg = body.get("storage_resource_group", "")
    account_name = body.get("account_name", "")
    db_name = body.get("db_name", "core_nt")
    if not all([sub, storage_rg, account_name]):
        return _error_response(400, "subscription_id, storage_resource_group, account_name required")
    if err := _validate_name(db_name, _RE_DB_NAME, "db_name"):
        return _error_response(400, err)
    if err := _validate_name(account_name, _RE_STORAGE_ACCOUNT, "account_name"):
        return _error_response(400, err)

    cred = credential_for_caller(identity.raw_token)
    try:
        from xml.etree import ElementTree

        s3_base = "https://ncbi-blast-databases.s3.amazonaws.com"

        # 1. Resolve the latest version directory
        latest_resp = _requests.get(f"{s3_base}/latest-dir", timeout=15)
        latest_resp.raise_for_status()
        latest_dir = latest_resp.text.strip()
        LOGGER.info("NCBI BLAST DB latest dir: %s", latest_dir)

        # 2. List matching objects under {latest_dir}/{db_name}*
        prefix = f"{latest_dir}/{db_name}"
        all_keys: list[str] = []
        continuation = ""
        max_pages = 50  # guard against unbounded S3 listing
        for _page in range(max_pages):
            list_url = f"{s3_base}?list-type=2&prefix={prefix}&max-keys=1000"
            if continuation:
                list_url += f"&continuation-token={continuation}"
            resp = _requests.get(list_url, timeout=30)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
            for el in root.findall(".//s3:Contents/s3:Key", ns):
                if el.text and not el.text.endswith("/"):
                    all_keys.append(el.text)
            is_truncated = root.findtext("s3:IsTruncated", "false", ns)
            if is_truncated == "true":
                token_el = root.find("s3:NextContinuationToken", ns)
                continuation = token_el.text if token_el is not None and token_el.text else ""
            else:
                break

        if not all_keys:
            return _error_response(404, f"No files found for database '{db_name}' in NCBI S3 (dir: {latest_dir})")

        # 2.5 Enable public network access (required for start_copy_from_url from NCBI S3)
        try:
            from azure.mgmt.storage import StorageManagementClient
            storage_mgmt = StorageManagementClient(cred, sub)
            storage_mgmt.storage_accounts.update(
                storage_rg, account_name,
                {"properties": {"public_network_access": "Enabled"}},
            )
            LOGGER.info("Temporarily enabled public access on %s for DB download", account_name)
            import time as _time
            _time.sleep(10)  # wait for propagation
        except Exception as toggle_exc:
            LOGGER.warning("Could not enable public access (may already be enabled): %s", str(toggle_exc)[:100])

        # 3. Background-start all copies. For large DBs this would exceed the
        #    SWA proxy timeout if done synchronously.
        from azure.storage.blob import BlobServiceClient
        blob_svc = BlobServiceClient(
            account_url=f"https://{account_name}.blob.core.windows.net",
            credential=cred,
        )
        container = blob_svc.get_container_client("blast-db")

        def _do_copies():
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import json as _json_mod
            from datetime import datetime as _dt, timezone as _tz

            def _copy_one(key: str) -> tuple[str, str]:
                source_url = f"{s3_base}/{key}"
                # Layout MUST match `elastic-blast` upstream
                # `util.py:get_blastdb_info`: it calls `os.path.dirname(db_url)`
                # and runs `azcopy list`, then filters lines containing
                # `os.path.basename(db)`. That requires files to live in a
                # subfolder named after the DB
                # (`blast-db/<db>/<files>`). A flat layout makes
                # `azcopy list` of the parent return wrong results and
                # elastic-blast reports "BLAST database … was not found".
                file_basename = key.split("/")[-1]
                blob_name = f"{db_name}/{file_basename}"
                try:
                    container.get_blob_client(blob_name).start_copy_from_url(source_url)
                    return (blob_name, "started")
                except Exception as e:
                    if "PendingCopyOperation" in str(e):
                        return (blob_name, "skipped")
                    LOGGER.warning("Copy failed for %s: %s", blob_name, str(e)[:200])
                    return (blob_name, "error")

            started = 0
            skipped = 0
            errors = 0
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

            LOGGER.info("DB prepare done for %s: %d started, %d skipped, %d errors", db_name, started, skipped, errors)

            # Write metadata blob
            try:
                metadata_blob = container.get_blob_client(f"{db_name}-metadata.json")
                metadata_blob.upload_blob(
                    _json_mod.dumps({
                        "db_name": db_name,
                        "source_version": latest_dir,
                        "downloaded_at": _dt.now(_tz.utc).isoformat(),
                        "file_count": started + skipped,
                    }).encode("utf-8"),
                    overwrite=True,
                )
            except Exception as e:
                LOGGER.warning("metadata write failed: %s", str(e)[:100])

            # Re-disable public access
            try:
                from azure.mgmt.storage import StorageManagementClient as _SM
                _sm = _SM(cred, sub)
                _sm.storage_accounts.update(
                    storage_rg, account_name,
                    {"properties": {"public_network_access": "Disabled"}},
                )
                LOGGER.info("Re-disabled public access on %s", account_name)
            except Exception as e:
                LOGGER.warning("Could not re-disable public access on %s: %s", account_name, str(e)[:100])

        from threading import Thread
        Thread(target=_do_copies, daemon=True).start()

        return _json_response({
            "ok": True,
            "db_name": db_name,
            "files_copied": 0,  # async — actual count tracked by client polling list_databases
            "files_total": len(all_keys),
            "source_version": latest_dir,
            "output": f"Started background copy of {len(all_keys)} files from {latest_dir}. Poll /blast/databases for progress.",
            "async": True,
        })
    except Exception as exc:
        LOGGER.warning("DB prepare failed: %s", exc)
        return _error_response(500, sanitise(str(exc)))
