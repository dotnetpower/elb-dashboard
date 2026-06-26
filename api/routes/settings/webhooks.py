"""Settings → Webhook notifications routes (deployment-wide outbound webhook).

Responsibility: HTTP shaping for reading/writing the webhook notification config
and sending a test message. Validation + masking live in
``api.services.webhooks_pref``; the send path lives in ``api.tasks.webhooks``.
Edit boundaries: Every route enforces ``require_caller``. The stored URL is a
secret — responses always return the masked form, never the raw URL.
Key entry points: ``get_webhooks``, ``put_webhooks``, ``test_webhook``.
Risky contracts: ``put`` rejects an SSRF-failing URL (400) via
``validate_webhook_url``; ``test`` re-validates at send time.
Validation: ``uv run pytest -q api/tests/test_settings_webhooks.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import CallerIdentity, require_caller
from api.services.sanitise import sanitise

LOGGER = logging.getLogger(__name__)

router = APIRouter()


class WebhookBody(BaseModel):
    url: str = Field(default="", max_length=600)
    enabled: bool = False
    events: str = Field(default="terminal")


def _unset_response() -> dict[str, Any]:
    return {
        "configured": False,
        "url_masked": "",
        "enabled": False,
        "events": "terminal",
        "updated_at": "",
    }


@router.get("")
def get_webhooks(_caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Return the webhook config (URL masked)."""
    from api.services.webhooks_pref import get_config

    config = get_config()
    return config.public_dict() if config else _unset_response()


@router.put("")
def put_webhooks(
    body: WebhookBody,
    caller: CallerIdentity = Depends(require_caller),
) -> dict[str, Any]:
    """Save the webhook config. Rejects an SSRF-failing URL with 400."""
    from api.services.webhooks_pref import WebhookValidationError, save_config

    try:
        config = save_config(
            url=body.url,
            enabled=body.enabled,
            events=body.events,
            owner_oid=caller.object_id,
        )
    except WebhookValidationError as exc:
        raise HTTPException(
            400, detail={"code": "invalid_webhook_url", "message": sanitise(str(exc))[:200]}
        ) from exc
    return config.public_dict()


@router.post("/test")
def test_webhook(_caller: CallerIdentity = Depends(require_caller)) -> dict[str, Any]:
    """Send a test message to the configured webhook (does not require the gate)."""
    from api.services.webhooks_pref import get_config
    from api.tasks.webhooks import post_webhook

    config = get_config()
    if config is None or not config.url:
        raise HTTPException(
            400, detail={"code": "not_configured", "message": "no webhook URL configured"}
        )
    test_message = {
        "text": "\u2705 elb-dashboard webhook test",
        "content": "\u2705 elb-dashboard webhook test",
    }
    delivered = post_webhook(config.url, test_message)
    return {"delivered": delivered}
