"""Client for the sibling ElasticBLAST OpenAPI execution plane."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from api.services.sanitise import sanitise

_BASE_URL_ENV = "ELB_OPENAPI_BASE_URL"
_INTERNAL_AUTH_ENV = "ELB_OPENAPI_INTERNAL_TOKEN"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_STREAM_TIMEOUT = httpx.Timeout(30.0, read=300.0)


@dataclass(frozen=True)
class DownloadedFile:
    content: bytes
    media_type: str
    filename: str


@dataclass(frozen=True)
class StreamedFile:
    chunks: Iterator[bytes]
    media_type: str
    filename: str


def _base_url() -> str:
    value = os.environ.get(_BASE_URL_ENV, "").strip().rstrip("/")
    if not value:
        from api.services.openapi_runtime import get_openapi_base_url

        value = get_openapi_base_url()
    if not value:
        raise HTTPException(
            503,
            detail={
                "code": "openapi_not_configured",
                "message": f"{_BASE_URL_ENV} is not set and no OpenAPI runtime endpoint is cached",
            },
        )
    return value


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = os.environ.get(_INTERNAL_AUTH_ENV, "").strip()
    if token:
        headers["X-ELB-Internal-Token"] = token
    return headers


def _safe_filename(value: str) -> str:
    name = value.strip().strip('"') or "blast_result.xml"
    name = name.split("/", 1)[-1].split("\\", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:128]
    return name or "blast_result.xml"


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def _sanitise_detail(value: Any) -> Any:
    if isinstance(value, str):
        return sanitise(value[:1000])
    if isinstance(value, dict):
        return {str(k)[:100]: _sanitise_detail(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitise_detail(v) for v in value[:20]]
    return value


def _raise_upstream_error(exc: httpx.HTTPStatusError) -> None:
    try:
        exc.response.read()
    except httpx.HTTPError:
        pass
    try:
        detail: Any = _sanitise_detail(exc.response.json())
    except Exception:
        detail = {"code": "openapi_error", "message": sanitise(exc.response.text[:500])}
    raise HTTPException(exc.response.status_code, detail=detail) from exc


def submit_job(payload: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(
        base_url=_base_url(), timeout=_DEFAULT_TIMEOUT_SECONDS, headers=_headers()
    ) as client:
        try:
            resp = client.post("/api/v1/elastic-blast/submit", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_upstream_error(exc)
        except httpx.HTTPError as exc:
            raise HTTPException(
                503,
                detail={"code": "openapi_unreachable", "message": str(exc)[:300]},
            ) from exc
        return resp.json()


def get_job(job_id: str) -> dict[str, Any]:
    with httpx.Client(
        base_url=_base_url(), timeout=_DEFAULT_TIMEOUT_SECONDS, headers=_headers()
    ) as client:
        try:
            resp = client.get(f"/api/v1/elastic-blast/jobs/{_path_segment(job_id)}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_upstream_error(exc)
        except httpx.HTTPError as exc:
            raise HTTPException(
                503,
                detail={"code": "openapi_unreachable", "message": str(exc)[:300]},
            ) from exc
        return resp.json()


def list_jobs() -> dict[str, Any]:
    """List all jobs tracked by the external ElasticBLAST OpenAPI service.

    The legacy `/v1/jobs` endpoint is the only listing surface exposed by the
    sibling service today; the newer `/api/v1/elastic-blast/...` contract has
    submit/get/file but no list. The shape is `{"jobs": [...], "count": N}`.
    """

    with httpx.Client(
        base_url=_base_url(), timeout=_DEFAULT_TIMEOUT_SECONDS, headers=_headers()
    ) as client:
        try:
            resp = client.get("/v1/jobs")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_upstream_error(exc)
        except httpx.HTTPError as exc:
            raise HTTPException(
                503,
                detail={"code": "openapi_unreachable", "message": str(exc)[:300]},
            ) from exc
        return resp.json()


def download_file(job_id: str, file_id: str) -> DownloadedFile:
    with httpx.Client(base_url=_base_url(), timeout=_STREAM_TIMEOUT, headers=_headers()) as client:
        try:
            resp = client.get(
                f"/api/v1/elastic-blast/jobs/{_path_segment(job_id)}/files/{_path_segment(file_id)}"
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_upstream_error(exc)
        except httpx.HTTPError as exc:
            raise HTTPException(
                503,
                detail={"code": "openapi_unreachable", "message": str(exc)[:300]},
            ) from exc
    content_disposition = resp.headers.get("content-disposition", "")
    filename = "blast_result.xml"
    if "filename=" in content_disposition:
        filename = _safe_filename(content_disposition.split("filename=", 1)[1])
    return DownloadedFile(
        content=resp.content,
        media_type=resp.headers.get("content-type", "application/xml").split(";", 1)[0],
        filename=filename,
    )


def stream_file(job_id: str, file_id: str) -> StreamedFile:
    client = httpx.Client(base_url=_base_url(), timeout=_STREAM_TIMEOUT, headers=_headers())
    try:
        request = client.build_request(
            "GET",
            f"/api/v1/elastic-blast/jobs/{_path_segment(job_id)}/files/{_path_segment(file_id)}",
        )
        resp = client.send(request, stream=True)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        client.close()
        _raise_upstream_error(exc)
    except httpx.HTTPError as exc:
        client.close()
        raise HTTPException(
            503,
            detail={"code": "openapi_unreachable", "message": str(exc)[:300]},
        ) from exc

    content_disposition = resp.headers.get("content-disposition", "")
    filename = "blast_result.xml"
    if "filename=" in content_disposition:
        filename = _safe_filename(content_disposition.split("filename=", 1)[1])

    def _iter() -> Iterator[bytes]:
        try:
            yield from resp.iter_bytes()
        finally:
            resp.close()
            client.close()

    return StreamedFile(
        chunks=_iter(),
        media_type=resp.headers.get("content-type", "application/xml").split(";", 1)[0],
        filename=filename,
    )
