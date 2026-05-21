"""Runtime ElasticBLAST CLI overrides for terminal exec.

Responsibility: Install ElasticBLAST monkey patches without editing the sibling
elastic-blast-azure checkout during host-mode development.
Edit boundaries: Keep this file limited to terminal subprocess startup behavior; permanent
source patches belong in terminal/patch_elastic_blast.py.
Key entry points: `_patch_azure_submit_cleanup`, `_patch_azure_blob_io`.
Risky contracts: Only activate when explicit ELB_DASHBOARD_* env flags are set so unrelated
Python processes are unchanged.
Validation: `uv run pytest -q api/tests/test_terminal_runtime_overrides.py`.
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

_LOGGER = logging.getLogger("elb_dashboard.elastic_blast_overrides")
_AZURE_CREDENTIAL: Any | None = None


def _patch_azure_submit_cleanup() -> None:
    if os.environ.get("ELB_DASHBOARD_FAST_JSON_SUBMIT_CLEANUP") != "1":
        return
    try:
        from elastic_blast import azure_cli_glue
    except Exception:
        return

    original = azure_cli_glue.submit_command
    if getattr(original, "_elb_dashboard_fast_cleanup", False):
        return

    def submit_command(
        args: Any,
        cfg: Any,
        clean_up_stack: list[Callable[..., Any]],
        *,
        default_submit: Callable[..., int],
    ) -> int:
        return_code = original(args, cfg, clean_up_stack, default_submit=default_submit)
        if bool(getattr(args, "json", False)) and return_code == 0:
            clean_up_stack.clear()
        return return_code

    submit_command._elb_dashboard_fast_cleanup = True  # type: ignore[attr-defined]
    azure_cli_glue.submit_command = submit_command


def _azure_fast_io_enabled() -> bool:
    return os.environ.get("ELB_DASHBOARD_FAST_AZURE_IO") == "1"


def _azure_credential() -> Any:
    global _AZURE_CREDENTIAL
    if _AZURE_CREDENTIAL is not None:
        return _AZURE_CREDENTIAL

    from azure.identity import AzureCliCredential, ChainedTokenCredential, ManagedIdentityCredential

    client_id = os.environ.get("AZURE_CLIENT_ID") or None
    managed_identity = ManagedIdentityCredential(client_id=client_id)
    azure_cli = AzureCliCredential()
    if os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"):
        _AZURE_CREDENTIAL = ChainedTokenCredential(managed_identity, azure_cli)
    else:
        _AZURE_CREDENTIAL = ChainedTokenCredential(azure_cli, managed_identity)
    return _AZURE_CREDENTIAL


def _split_blob_url(blob_url: str, sas_token: str | None = None) -> tuple[str, str, str, str]:
    parsed = urlsplit(blob_url)
    path_parts = parsed.path.lstrip("/").split("/", 1)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(path_parts) != 2:
        raise ValueError(f"not an Azure Blob URL: {blob_url}")
    query = parsed.query or (sas_token or "").lstrip("?")
    account_url = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return account_url, path_parts[0], unquote(path_parts[1]), query


def _blob_client(blob_url: str, sas_token: str | None = None) -> Any:
    from azure.storage.blob import BlobClient

    account_url, container_name, blob_name, query = _split_blob_url(blob_url, sas_token)
    if query:
        parsed = urlsplit(blob_url)
        blob_url_with_query = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)
        )
        return BlobClient.from_blob_url(blob_url_with_query)
    return BlobClient(
        account_url=account_url,
        container_name=container_name,
        blob_name=blob_name,
        credential=_azure_credential(),
    )


def _container_client(blob_url: str, sas_token: str | None = None) -> Any:
    from azure.storage.blob import ContainerClient

    account_url, container_name, _blob_name, query = _split_blob_url(blob_url, sas_token)
    if query:
        parsed = urlsplit(blob_url)
        container_path = "/" + container_name
        container_url = urlunsplit((parsed.scheme, parsed.netloc, container_path, query, ""))
        return ContainerClient.from_container_url(container_url)
    return ContainerClient(
        account_url=account_url,
        container_name=container_name,
        credential=_azure_credential(),
    )


def _azure_blob_length(blob_url: str, sas_token: str | None = None) -> int:
    properties = _blob_client(blob_url, sas_token).get_blob_properties()
    return int(getattr(properties, "size", properties["size"]))


def _azure_blob_readall(blob_url: str, sas_token: str | None = None) -> bytes:
    return bytes(_blob_client(blob_url, sas_token).download_blob().readall())


def _azure_list_matching_db_blobs(db_url: str, sas_token: str | None = None) -> list[str]:
    _account_url, _container_name, blob_name, _query = _split_blob_url(db_url, sas_token)
    blob_prefix = blob_name[:-2] if blob_name.endswith(".*") else blob_name
    container_client = _container_client(db_url, sas_token)
    return [str(blob.name) for blob in container_client.list_blobs(name_starts_with=blob_prefix)]


def _call_original(original: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return original(*args, **kwargs)


def _patch_azure_blob_io() -> None:
    if not _azure_fast_io_enabled():
        return
    try:
        from elastic_blast import filehelper, util
        from elastic_blast.constants import ELB_AZURE_PREFIX
    except Exception:
        return

    if getattr(filehelper, "_elb_dashboard_fast_azure_io", False):
        return

    original_get_length = filehelper.get_length
    original_check_for_read = filehelper.check_for_read
    original_open_for_read = filehelper.open_for_read
    original_open_for_write_immediate = filehelper.open_for_write_immediate
    original_check_user_provided_blastdb_exists = util.check_user_provided_blastdb_exists
    original_get_blastdb_info = util.get_blastdb_info

    def get_length(
        fname: str,
        dry_run: bool = False,
        gcp_prj: str | None = None,
        sas_token: str | None = None,
    ) -> int:
        if fname.startswith(ELB_AZURE_PREFIX) and not dry_run:
            try:
                return _azure_blob_length(fname, sas_token)
            except Exception as exc:
                _LOGGER.debug("Azure SDK length fallback for %s: %s", fname, exc)
        return _call_original(
            original_get_length,
            fname,
            dry_run=dry_run,
            gcp_prj=gcp_prj,
            sas_token=sas_token,
        )

    def check_for_read(
        fname: str,
        dry_run: bool = False,
        print_file_size: bool = False,
        gcp_prj: str | None = None,
        sas_token: str | None = None,
    ) -> None:
        if fname.startswith(ELB_AZURE_PREFIX) and not dry_run:
            try:
                size = _azure_blob_length(fname, sas_token)
                if print_file_size:
                    _LOGGER.debug("%s size %s", fname, size)
                return
            except Exception as exc:
                _LOGGER.debug("Azure SDK read-check fallback for %s: %s", fname, exc)
        _call_original(
            original_check_for_read,
            fname,
            dry_run=dry_run,
            print_file_size=print_file_size,
            gcp_prj=gcp_prj,
            sas_token=sas_token,
        )

    def open_for_read(fname: str, gcp_prj: str | None = None, sas_token: str | None = None) -> Any:
        if fname.startswith(ELB_AZURE_PREFIX):
            try:
                data = _azure_blob_readall(fname, sas_token)
                gzipped = fname[-3:] == ".gz"
                tarred = filehelper.re.match(r"^.*\.(tar(|\.gz|\.bz2)|tgz)$", fname) is not None
                if gzipped or tarred:
                    return filehelper.unpack_stream(io.BytesIO(data), gzipped, tarred)
                return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
            except Exception as exc:
                _LOGGER.debug("Azure SDK read fallback for %s: %s", fname, exc)
        return _call_original(original_open_for_read, fname, gcp_prj=gcp_prj, sas_token=sas_token)

    @contextmanager
    def open_for_write_immediate(fname: str, sas_token: str | None = None) -> Any:
        if not fname.startswith(ELB_AZURE_PREFIX):
            with original_open_for_write_immediate(fname, sas_token=sas_token) as output_handle:
                yield output_handle
            return

        buffer = io.BytesIO()
        output_handle = io.TextIOWrapper(buffer, encoding="utf-8")
        try:
            yield output_handle
            output_handle.flush()
            data = buffer.getvalue()
            try:
                _blob_client(fname, sas_token).upload_blob(data, overwrite=True)
            except Exception as exc:
                _LOGGER.debug("Azure SDK write fallback for %s: %s", fname, exc)
                with original_open_for_write_immediate(fname, sas_token=sas_token) as fallback:
                    fallback.write(data.decode("utf-8"))
        except Exception as exc:
            _LOGGER.debug("Azure SDK write failed for %s: %s", fname, exc)
            raise
        finally:
            try:
                output_handle.detach()
            except Exception as exc:
                _LOGGER.debug("Azure SDK write buffer detach failed for %s: %s", fname, exc)

    def check_user_provided_blastdb_exists(
        db: str,
        mol_type: Any,
        db_source: Any,
        gcp_prj: str | None = None,
        sas_token: str | None = None,
    ) -> None:
        if db.startswith(ELB_AZURE_PREFIX):
            try:
                if _azure_list_matching_db_blobs(db, sas_token):
                    return
                raise ValueError(f"BLAST database {db} was not found")
            except ValueError:
                raise
            except Exception as exc:
                _LOGGER.debug("Azure SDK DB check fallback for %s: %s", db, exc)
        return _call_original(
            original_check_user_provided_blastdb_exists,
            db,
            mol_type,
            db_source,
            gcp_prj=gcp_prj,
            sas_token=sas_token,
        )

    def get_blastdb_info(
        blastdb: str,
        gcp_prj: str | None = None,
        sas_token: str | None = None,
    ) -> tuple[str, str, str]:
        if blastdb.startswith(ELB_AZURE_PREFIX):
            try:
                db_url = blastdb[:-2] if blastdb.endswith(".*") else blastdb
                matches = _azure_list_matching_db_blobs(db_url, sas_token)
                if not matches:
                    raise ValueError(f"There are no files at the bucket {db_url}.*")
                db_name = os.path.basename(urlsplit(db_url).path)
                if any(blob_name.endswith("tar.gz") for blob_name in matches):
                    db_path = db_url + ".tar.gz"
                elif sas_token:
                    db_path = f"{os.path.dirname(db_url)}/*?{sas_token.lstrip('?')}"
                else:
                    db_path = f"{os.path.dirname(db_url)}/*"
                return db_name, db_path, util.sanitize_for_k8s(db_name)
            except ValueError:
                raise
            except Exception as exc:
                _LOGGER.debug("Azure SDK DB info fallback for %s: %s", blastdb, exc)
        return _call_original(
            original_get_blastdb_info,
            blastdb,
            gcp_prj=gcp_prj,
            sas_token=sas_token,
        )

    filehelper.get_length = get_length
    filehelper.check_for_read = check_for_read
    filehelper.open_for_read = open_for_read
    filehelper.open_for_write_immediate = open_for_write_immediate
    util.check_user_provided_blastdb_exists = check_user_provided_blastdb_exists
    util.get_blastdb_info = get_blastdb_info
    try:
        from elastic_blast.commands import submit as submit_module
    except Exception as exc:
        _LOGGER.debug("ElasticBLAST submit module was not patched yet: %s", exc)
    else:
        submit_module.get_length = get_length
        submit_module.check_for_read = check_for_read
        submit_module.open_for_read = open_for_read
        submit_module.open_for_write_immediate = open_for_write_immediate
        submit_module.check_user_provided_blastdb_exists = check_user_provided_blastdb_exists
    filehelper._elb_dashboard_fast_azure_io = True


_patch_azure_submit_cleanup()
_patch_azure_blob_io()
