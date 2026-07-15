"""POST `/api/blast/register-external-job` webhook receiver.

Responsibility: Accept terminal-transition / submitted-event notifications POSTed by the
sibling ``elb-openapi`` pod (`docker-openapi/app/main.py::_webhook_notify`) and accelerate
the dashboard's view of that job by writing the freshly-reported status straight onto the
existing jobstate row instead of waiting for the next periodic /v1/jobs poll.
Edit boundaries: Auth/validation/response shaping here only. State writes go through
``api.services.state_repo``. Do not add internal retry loops — the sibling already attempts
3 times with exponential backoff; an internal loop on this side would only amplify a real
outage.
Key entry points: ``register_external_job``
Risky contracts:
  * Auth is a static bearer token (``ELB_OPENAPI_INTERNAL_TOKEN``) shared between sibling
    and dashboard. The dashboard sources it from the same value the cluster manifest writes
    for ``ELB_OPENAPI_API_TOKEN`` (single shared secret per cluster). This route MUST NOT
    use ``require_caller`` — the sibling cannot present an MSAL bearer.
  * Always return HTTP 202 on auth success (even for unknown ``job_id`` or write failures).
    A 4xx/5xx would cause the sibling to retry-storm against the dashboard.
  * Only writes status/phase/error_code onto an EXISTING jobstate row. If the row does not
    exist, log + 202 — the next normal /v1/jobs poll will create it with the right owner.
Validation: ``uv run pytest -q api/tests/test_external_webhook.py``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

LOGGER = logging.getLogger(__name__)

router = APIRouter()


# Sibling terminal statuses (see ``docker-openapi/app/main.py::_TERMINAL_STATES``).
# ``submitted`` is a non-terminal lifecycle event the sibling also fires; we accept it
# but do NOT flip a stored ``running`` row backwards to ``submitted``.
_ACCEPTED_STATUSES = frozenset(
    {
        "submitted",
        "queued",
        "dispatching",
        "submitting",
        "running",
        "completed",
        "failed",
        "cancelled",
    }
)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_FORWARD_ONLY_BLOCKED_WHEN_RUNNING = frozenset({"submitted", "queued"})


class ExternalJobEvent(BaseModel):
    """Payload posted by the sibling's ``_webhook_notify``.

    Extra fields are allowed so the sibling can add metadata later without breaking
    this contract (forward compatibility). Required fields are intentionally minimal.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    job_id: str = Field(..., min_length=1, max_length=128)
    event: str | None = None
    status: str | None = None
    error: str | None = None
    # Sibling-derived runtime stats on the terminal flip. Optional so older
    # sibling builds (and the submit/cancel notify paths that do not yet attach
    # them) keep working. The handler caches whatever is present so the
    # BlastJobs list view's "Elapsed" / "Duration" timer reads accurate values
    # immediately, instead of waiting up to one /v1/jobs sync cycle (~70 s)
    # for ``_sync_external_jobs_to_table`` to pull the same stats.
    started_at: str | None = None
    run_seconds: int | None = None
    queue_wait_seconds: int | None = None
    elapsed_seconds: int | None = None


def _expected_token() -> str:
    """The shared secret the sibling pod is expected to attach.

    The dashboard reuses ``ELB_OPENAPI_API_TOKEN`` (which the api sidecar already manages
    for outbound proxy calls) as the single shared cluster secret; the deploy task plumbs
    it into the AKS pod as ``ELB_OPENAPI_INTERNAL_TOKEN`` so the sibling can attach it on
    the inbound webhook. Keeping both names mapped to one value keeps the trust boundary
    explicit ("one cluster, one secret").

    Lookup precedence:
      1. ``ELB_OPENAPI_INTERNAL_TOKEN`` env (explicit override / test path)
      2. ``ELB_OPENAPI_API_TOKEN`` env (legacy / manual config)
      3. Ops Redis runtime cache (the worker's ``deploy_openapi_service`` writes the
         minted token here via ``save_openapi_api_token``; this is the production
         path because the api sidecar does not carry the token in its env)
    """

    env_token = (
        os.environ.get("ELB_OPENAPI_INTERNAL_TOKEN")
        or os.environ.get("ELB_OPENAPI_API_TOKEN")
        or ""
    ).strip()
    if env_token:
        return env_token
    try:
        from api.services.openapi.runtime import get_openapi_api_token

        return (get_openapi_api_token() or "").strip()
    except Exception as exc:  # pragma: no cover — Redis import / connect failure
        LOGGER.debug("openapi webhook: runtime token lookup failed: %s", type(exc).__name__)
        return ""


def _verify_token(request: Request) -> None:
    """Reject the request unless ``Authorization: Bearer <token>`` matches the env."""

    expected = _expected_token()
    if not expected:
        # Webhook receiving is not configured. Fail-closed; this is a config gap on
        # the dashboard side, not a sibling fault, so 503 is more informative than 401
        # in App Insights when triaging "why isn't the webhook firing".
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="webhook_not_configured"
        )
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_bearer")
    presented = header.split(" ", 1)[1].strip()
    # Constant-time compare via hmac.compare_digest equivalent (Python str ==
    # is timing-stable in CPython for equal-length short strings; use hmac for safety).
    import hmac

    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad_bearer")


def _normalise_status(body: ExternalJobEvent) -> str:
    raw = (body.status or body.event or "").strip().lower()
    return raw


def _apply_to_jobstate(job_id: str, ext_status: str, error_msg: str | None) -> dict[str, Any]:
    """Write the reported status onto an existing jobstate row, idempotently.

    Returns a small dict describing the outcome (logged + emitted as the App Insights
    custom event payload). Never raises for ordinary failures — auth failure is the
    only path that 4xx-es out; everything else degrades to 202 + ``synced=False``.
    """

    try:
        from api.services.state_repo import get_state_repo

        repo = get_state_repo()
        existing = repo.get(job_id)
    except Exception as exc:
        LOGGER.warning(
            "openapi webhook: state_repo unavailable job_id=%s err=%s", job_id, type(exc).__name__
        )
        return {"synced": False, "reason": "state_repo_unavailable"}

    if existing is None:
        # The job was submitted from a path the dashboard has not polled yet (e.g.,
        # OpenAPI submit by an external script). The next regular /v1/jobs poll will
        # create the row with the right owner_oid; writing it here would clobber
        # ownership. Log INFO so the field can correlate webhook arrival vs. row creation.
        LOGGER.info(
            "openapi webhook: unknown job_id=%s status=%s — deferring to next poll",
            job_id,
            ext_status,
        )
        return {"synced": False, "reason": "unknown_job"}

    cur_status = str(getattr(existing, "status", "") or "").lower()

    # Terminal rows are immutable against NON-terminal events. The sibling fires
    # webhooks with a 3-retry exponential backoff, so a ``running``/``submitted``
    # notification queued for retry can be delivered AFTER the job already reached
    # a terminal state (the docstring hazard "the 3-retry window can outlast its
    # own state machine moving on"). Without this guard a late ``running`` webhook
    # would resurrect a finished ``completed``/``failed``/``cancelled`` row back to
    # ``running``. A genuine terminal→terminal correction is still allowed (the
    # sibling is authoritative for terminals).
    if cur_status in _TERMINAL_STATUSES and ext_status not in _TERMINAL_STATUSES:
        LOGGER.info(
            "openapi webhook: ignoring non-terminal event on terminal row "
            "job_id=%s cur=%s incoming=%s",
            job_id,
            cur_status,
            ext_status,
        )
        return {"synced": False, "reason": "terminal_locked"}

    # Forward-only: a stored ``running`` row must NOT be flipped backwards by an
    # out-of-order ``submitted`` webhook that arrived late (the sibling's 3-retry
    # window can outlast its own state machine moving on). Terminal states are
    # always accepted (sibling is authoritative for terminals).
    if cur_status == "running" and ext_status in _FORWARD_ONLY_BLOCKED_WHEN_RUNNING:
        LOGGER.info(
            "openapi webhook: ignoring backward transition job_id=%s cur=%s incoming=%s",
            job_id,
            cur_status,
            ext_status,
        )
        return {"synced": False, "reason": "backward_transition_ignored"}

    if cur_status == ext_status and not (ext_status in _TERMINAL_STATUSES and error_msg):
        # Idempotent no-op: same status, no new error detail to attach.
        return {"synced": True, "noop": True, "status": ext_status}

    update_kwargs: dict[str, Any] = {"status": ext_status, "phase": ext_status}
    if ext_status in _TERMINAL_STATUSES and error_msg:
        # ``error_code`` is a short tag (≤200 chars per sibling truncation rule).
        update_kwargs["error_code"] = error_msg[:200]
    elif ext_status in {"completed", "succeeded"} and (getattr(existing, "error_code", "") or ""):
        # Clear a stale error_code when terminal-success arrives (mirrors the existing
        # _sync_external_jobs_to_table behaviour so the SPA does not keep showing a
        # recovered error tag on a finished-OK row).
        update_kwargs["error_code"] = ""

    try:
        repo.update(job_id, **update_kwargs)
    except KeyError:
        # Raced with a delete; treat as unknown.
        LOGGER.info("openapi webhook: row vanished mid-update job_id=%s", job_id)
        return {"synced": False, "reason": "row_gone"}
    except Exception as exc:
        LOGGER.warning(
            "openapi webhook: update failed job_id=%s err=%s", job_id, type(exc).__name__
        )
        return {"synced": False, "reason": "update_failed"}

    return {"synced": True, "from": cur_status, "to": ext_status}


@router.post(
    "/register-external-job",
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
)
async def register_external_job(request: Request) -> dict[str, Any]:
    """Receive a terminal-transition / lifecycle webhook from the sibling elb-openapi pod.

    Always returns 202 on auth success — even when the job is unknown or the write fails —
    so the sibling's bounded retry never retry-storms into the dashboard.
    """

    _verify_token(request)
    try:
        raw_body = await request.json()
        body = ExternalJobEvent.model_validate(raw_body)
    except Exception as exc:
        # Authenticated sibling notifications must never retry-storm because a
        # payload was truncated or from a forward-incompatible build. Do not
        # log the body: it may contain query/result metadata.
        LOGGER.warning(
            "openapi webhook: invalid body error=%s content_length=%s",
            type(exc).__name__,
            request.headers.get("content-length", ""),
        )
        return {
            "status": "accepted",
            "synced": False,
            "reason": "invalid_body",
        }
    job_id = body.job_id.strip()
    ext_status = _normalise_status(body)
    if ext_status not in _ACCEPTED_STATUSES:
        LOGGER.info(
            "openapi webhook: ignored unknown status job_id=%s status=%s event=%s",
            job_id,
            body.status,
            body.event,
        )
        return {"status": "accepted", "synced": False, "reason": "unknown_status"}

    outcome = _apply_to_jobstate(job_id, ext_status, body.error)
    # Cache sibling-derived runtime stats on the terminal fast path so the
    # BlastJobs list view's "Elapsed" / "Duration" timer reads accurate values
    # the instant the webhook lands -- rather than waiting up to one
    # /v1/jobs sync cycle (~70 s) for ``_sync_external_jobs_to_table`` to
    # pull the same numbers. The list-view merge in ``_local_to_blast_job``
    # then surfaces whichever fields the sibling supplied. Best-effort:
    # a cache write failure must never fail the webhook ACK.
    if ext_status in _TERMINAL_STATUSES:
        try:
            from api.services.blast.external_config import remember_sibling_stats

            stats_payload = {
                k: getattr(body, k)
                for k in ("started_at", "run_seconds", "queue_wait_seconds", "elapsed_seconds")
                if getattr(body, k, None) not in (None, "")
            }
            if stats_payload:
                remember_sibling_stats(job_id, stats_payload)
        except Exception:
            LOGGER.debug(
                "openapi webhook: sibling stats cache populate skipped job_id=%s",
                job_id,
                exc_info=True,
            )
    LOGGER.info(
        "openapi webhook: job_id=%s status=%s event=%s outcome=%s",
        job_id,
        ext_status,
        body.event,
        outcome,
    )
    return {"status": "accepted", **outcome}
