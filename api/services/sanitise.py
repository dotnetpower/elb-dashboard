"""Output sanitisation helpers.

Used before returning shell command output (Run Command, kubectl, az) to
the SPA so we never echo bearer tokens, SAS query strings, or full secret
URIs even if a downstream tool happens to log them.
"""

from __future__ import annotations

import re

# Patterns we mask in any user-facing string.
_SAS_RE = re.compile(r"\?(?:[A-Za-z0-9_-]+=[^&\s]+&){2,}[^&\s]+")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}")
_AZURE_KEY_RE = re.compile(
    r"(?i)(account[-_]?key|access[-_]?key|client[-_]?secret)[\"'\s:=]+[A-Za-z0-9+/=_-]{20,}"
)
_GUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def sanitise(text: str | None, *, mask_subscription_ids: bool = True) -> str:
    """Mask sensitive substrings in `text`.

    - SAS query strings → `?<sas-redacted>`
    - Bearer tokens     → `Bearer <redacted>`
    - account/access keys, client secrets → `<key>=<redacted>`
    - Azure GUIDs       → first 8 chars + `…` (subscriptions, tenant ids)
                          when `mask_subscription_ids=True`.
    """
    if not text:
        return ""
    out = text
    out = _SAS_RE.sub("?<sas-redacted>", out)
    out = _BEARER_RE.sub("Bearer <redacted>", out)
    out = _AZURE_KEY_RE.sub(r"\1=<redacted>", out)
    if mask_subscription_ids:
        out = _GUID_RE.sub(lambda m: m.group(0)[:8] + "…", out)
    return out
