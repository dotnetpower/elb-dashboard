"""Azure Resource Group tag validation rules for the `/api/arm/resource-group/tags` route.

Responsibility: Validate ARM tag names/values against Azure's documented limits at the api
boundary so a malformed POST cannot turn into an Azure SDK exception that leaks request ids /
server messages into the response body.
Edit boundaries: Pure validation rules — no Azure SDK, no caching, no response shaping beyond
raising `HTTPException(400, ...)` at the boundary. The route owns which tag *keys* the dashboard
may write (the `elb-` prefix policy); this module owns tag *format* (length / forbidden chars /
control chars / count).
Key entry points: `validate_tag_name`, `validate_tag_value`, `TAG_MAX_PER_REQUEST`.
Risky contracts: Error messages and HTTP 400 status must stay byte-for-byte identical to the
historical inline checks — the SPA and route-contract tests assert on them.
Validation: `uv run pytest -q api/tests/test_route_contracts.py`.

Azure ARM tag limits (Microsoft Learn):
- Tag name: 1..512 characters; cannot contain ``<>%&\\?/``
- Tag value: 0..256 characters
- Tags per resource: max 50
"""

from __future__ import annotations

from fastapi import HTTPException

TAG_NAME_MAX_LEN = 512
TAG_VALUE_MAX_LEN = 256
TAG_MAX_PER_REQUEST = 50
TAG_NAME_FORBIDDEN_CHARS = set("<>%&\\?/")


def validate_tag_name(key: str) -> None:
    """Raise ``HTTPException(400, ...)`` if ``key`` violates Azure tag-name rules."""
    if not key:
        raise HTTPException(400, "tag name must not be empty")
    if len(key) > TAG_NAME_MAX_LEN:
        raise HTTPException(
            400, f"tag name exceeds {TAG_NAME_MAX_LEN} characters: {key[:40]}..."
        )
    bad = TAG_NAME_FORBIDDEN_CHARS.intersection(key)
    if bad:
        raise HTTPException(
            400,
            f"tag name {key!r} contains characters Azure rejects: {sorted(bad)}",
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
        raise HTTPException(400, f"tag name {key!r} contains control characters")


def validate_tag_value(key: str, value: str) -> None:
    """Raise ``HTTPException(400, ...)`` if ``value`` violates Azure tag-value rules."""
    if value is None:
        return
    if not isinstance(value, str):
        raise HTTPException(400, f"tag value for {key!r} must be a string")
    if len(value) > TAG_VALUE_MAX_LEN:
        raise HTTPException(
            400,
            f"tag value for {key!r} exceeds {TAG_VALUE_MAX_LEN} characters",
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise HTTPException(400, f"tag value for {key!r} contains control characters")
