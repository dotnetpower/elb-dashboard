"""Client for the sibling ElasticBLAST OpenAPI execution plane.

Responsibility: Client for the sibling ElasticBLAST OpenAPI execution plane
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `DownloadedFile`, `StreamedFile`, `_base_url`, `submit_job`, `get_job`,
`list_jobs`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from api.services.sanitise import sanitise

_BASE_URL_ENV = "ELB_OPENAPI_BASE_URL"
_INTERNAL_AUTH_ENV = "ELB_OPENAPI_INTERNAL_TOKEN"
_API_AUTH_ENV = "ELB_OPENAPI_API_TOKEN"
# Submit is the slowest sibling call: it creates a ConfigMap + applies a
# K8s Job + waits for the AKS API server to ack. Under cold cache /
# AKS API server pressure 30 s is not enough — the request times out
# locally but the sibling has already created the job on the cluster,
# leaving an orphan job the dashboard does not know about. 90 s covers
# the observed worst-case while keeping the user-perceived wait bounded.
_DEFAULT_TIMEOUT_SECONDS = 90.0
_LIST_TIMEOUT_SECONDS = float(os.environ.get("OPENAPI_LIST_TIMEOUT_SECONDS", "5.0"))
_STREAM_TIMEOUT = httpx.Timeout(30.0, read=300.0)
# Submit retries on transient transport failures (connection refused /
# reset / read timeout). Only applied when the payload carries an
# idempotency_key so the sibling can dedupe a re-send; without it we
# would risk creating duplicate jobs on the cluster. Override at the env
# layer (``OPENAPI_SUBMIT_MAX_RETRIES=0``) to make a flaky test
# deterministic without waiting for the backoff sleeps.
_SUBMIT_MAX_TRANSPORT_RETRIES = int(os.environ.get("OPENAPI_SUBMIT_MAX_RETRIES", "2"))
_SUBMIT_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.5)
# Truncation policy for sibling-derived error messages we surface in 503
# ``openapi_unreachable`` responses. Kept at 300 chars verbatim from the
# pre-refactor sites; named so the policy is visible in one place.
_TRANSPORT_DETAIL_MAX_CHARS = 300
# Sanitise-detail truncation policy (kept verbatim from the pre-refactor
# inline literals so observable behaviour does not change).
_SANITISE_DETAIL_STRING_MAX_CHARS = 1000
_SANITISE_DETAIL_KEY_MAX_CHARS = 100
_SANITISE_DETAIL_LIST_LIMIT = 20
# Safe-filename cap. FAT32/NTFS allow up to 255 but the SPA + Storage
# manifest reserve the rest for path/extension prefixes.
_SAFE_FILENAME_MAX_LENGTH = 128
LOGGER = logging.getLogger(__name__)


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


def _base_url(value: str | None = None) -> str:
    value = (value or os.environ.get(_BASE_URL_ENV, "")).strip().rstrip("/")
    if not value:
        from api.services.openapi.runtime import get_openapi_base_url

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


def _headers(*, api_token: str | None = None, internal_token: str | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    api_token = (api_token or os.environ.get(_API_AUTH_ENV, "")).strip()
    if not api_token:
        from api.services.openapi.runtime import get_openapi_api_token

        api_token = get_openapi_api_token()
    if api_token:
        headers["X-ELB-API-Token"] = api_token
    token = (internal_token or os.environ.get(_INTERNAL_AUTH_ENV, "")).strip()
    if token:
        headers["X-ELB-Internal-Token"] = token
    return headers


def _safe_filename(value: str) -> str:
    name = value.strip().strip('"') or "blast_result.xml"
    name = name.split("/", 1)[-1].split("\\", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:_SAFE_FILENAME_MAX_LENGTH]
    return name or "blast_result.xml"


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def _sanitise_detail(value: Any) -> Any:
    if isinstance(value, str):
        return sanitise(value[:_SANITISE_DETAIL_STRING_MAX_CHARS])
    if isinstance(value, dict):
        return {
            str(k)[:_SANITISE_DETAIL_KEY_MAX_CHARS]: _sanitise_detail(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitise_detail(v) for v in value[:_SANITISE_DETAIL_LIST_LIMIT]]
    return value


def _compact_log_detail(value: Any, *, limit: int = 2000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return sanitise(text[:limit])


def _upstream_request_url(exc: httpx.HTTPStatusError) -> str:
    try:
        return sanitise(str(exc.request.url))
    except Exception:
        return ""


def _raise_upstream_error(exc: httpx.HTTPStatusError) -> None:
    try:
        exc.response.read()
    except httpx.HTTPError:
        pass
    try:
        detail: Any = _sanitise_detail(exc.response.json())
    except Exception:
        detail = {"code": "openapi_error", "message": sanitise(exc.response.text[:500])}
    if isinstance(detail, dict):
        detail = dict(detail)
        detail.setdefault("code", f"openapi_http_{exc.response.status_code}")
        detail.setdefault("upstream_status", exc.response.status_code)
        detail.setdefault("upstream_url", _upstream_request_url(exc))
    LOGGER.warning(
        "OpenAPI upstream request failed method=%s url=%s status=%s reason=%s "
        "content_type=%s request_id=%s detail=%s",
        exc.request.method,
        _upstream_request_url(exc),
        exc.response.status_code,
        exc.response.reason_phrase,
        exc.response.headers.get("content-type", ""),
        exc.response.headers.get("x-request-id")
        or exc.response.headers.get("x-ms-request-id")
        or exc.response.headers.get("x-correlation-id")
        or "",
        _compact_log_detail(detail),
    )
    raise HTTPException(exc.response.status_code, detail=detail) from exc


def submit_job(
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    api_token: str | None = None,
) -> dict[str, Any]:
    """POST the canonical submit body to the sibling OpenAPI service.

    When ``payload`` carries an ``idempotency_key`` (server-derived from
    ``external_correlation_id`` for dashboard submissions, or supplied by
    the external caller for direct API hits) the sibling can safely dedupe
    a re-send, so this client retries up to ``_SUBMIT_MAX_TRANSPORT_RETRIES``
    times on transient transport failures (httpx.ConnectError /
    httpx.ReadTimeout / etc.). Without an idempotency_key the call is
    surfaced to the caller on the first failure to avoid creating duplicate
    jobs on the cluster.
    """
    has_idempotency_key = bool(
        isinstance(payload, dict)
        and (
            payload.get("idempotency_key")
            or payload.get("external_correlation_id")
        )
    )
    import time as _time

    attempts = 1 + (_SUBMIT_MAX_TRANSPORT_RETRIES if has_idempotency_key else 0)
    last_transport_exc: HTTPException | None = None
    for attempt_index in range(attempts):
        with httpx.Client(
            base_url=_base_url(base_url),
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            headers=_headers(api_token=api_token),
        ) as client:
            try:
                resp = client.post("/api/v1/elastic-blast/submit", json=payload)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # 4xx/5xx with a response body: surface immediately. The
                # sibling already decided — retrying won't change it (and a
                # 5xx may itself indicate a created-but-failed job).
                _raise_upstream_error(exc)
            except httpx.HTTPError as exc:
                # Transport-level failure (connection refused / reset /
                # read timeout). Retry only when idempotent.
                transport_exc = HTTPException(
                    503,
                    detail={
                        "code": "openapi_unreachable",
                        "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
                        "attempt": attempt_index + 1,
                        "max_attempts": attempts,
                    },
                )
                last_transport_exc = transport_exc
                if attempt_index + 1 < attempts:
                    backoff = _SUBMIT_RETRY_BACKOFF_SECONDS[
                        min(attempt_index, len(_SUBMIT_RETRY_BACKOFF_SECONDS) - 1)
                    ]
                    LOGGER.info(
                        "openapi submit transport failure attempt=%s/%s sleep=%ss reason=%s",
                        attempt_index + 1,
                        attempts,
                        backoff,
                        type(exc).__name__,
                    )
                    _time.sleep(backoff)
                    continue
                raise transport_exc from exc
            return cast(dict[str, Any], resp.json())
    # Defensive: the loop above always either returns or raises. If we
    # somehow exit without doing either, surface the last transport error.
    if last_transport_exc is not None:
        raise last_transport_exc
    raise HTTPException(
        503,
        detail={"code": "openapi_unreachable", "message": "submit failed without explicit error"},
    )


def get_job(
    job_id: str,
    *,
    base_url: str | None = None,
    api_token: str | None = None,
) -> dict[str, Any]:
    with httpx.Client(
        base_url=_base_url(base_url),
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        headers=_headers(api_token=api_token),
    ) as client:
        try:
            resp = client.get(f"/api/v1/elastic-blast/jobs/{_path_segment(job_id)}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_upstream_error(exc)
        except httpx.HTTPError as exc:
            raise HTTPException(
                503,
                detail={
                    "code": "openapi_unreachable",
                    "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
                },
            ) from exc
        return cast(dict[str, Any], resp.json())


def list_jobs(*, base_url: str | None = None, api_token: str | None = None) -> dict[str, Any]:
    """List all jobs tracked by the external ElasticBLAST OpenAPI service.

    The legacy `/v1/jobs` endpoint is the only listing surface exposed by the
    sibling service today; the newer `/api/v1/elastic-blast/...` contract has
    submit/get/file but no list. The shape is `{"jobs": [...], "count": N}`.
    """

    with httpx.Client(
        base_url=_base_url(base_url),
        timeout=_LIST_TIMEOUT_SECONDS,
        headers=_headers(api_token=api_token),
    ) as client:
        try:
            resp = client.get("/v1/jobs")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_upstream_error(exc)
        except httpx.HTTPError as exc:
            raise HTTPException(
                503,
                detail={
                    "code": "openapi_unreachable",
                    "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
                },
            ) from exc
        return cast(dict[str, Any], resp.json())


def download_file(
    job_id: str,
    file_id: str,
    *,
    base_url: str | None = None,
    api_token: str | None = None,
) -> DownloadedFile:
    with httpx.Client(
        base_url=_base_url(base_url), timeout=_STREAM_TIMEOUT, headers=_headers(api_token=api_token)
    ) as client:
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
                detail={
                    "code": "openapi_unreachable",
                    "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
                },
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


def stream_file(
    job_id: str,
    file_id: str,
    *,
    base_url: str | None = None,
    api_token: str | None = None,
) -> StreamedFile:
    client = httpx.Client(
        base_url=_base_url(base_url), timeout=_STREAM_TIMEOUT, headers=_headers(api_token=api_token)
    )
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
            detail={
                "code": "openapi_unreachable",
                "message": sanitise(str(exc)[:_TRANSPORT_DETAIL_MAX_CHARS]),
            },
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
