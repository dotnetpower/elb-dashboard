"""Output sanitisation helpers.

Responsibility: Output sanitisation helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `sanitise`, `redact_oid`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests`.
"""

from __future__ import annotations

import hashlib
import re

# Patterns we mask in any user-facing string.
_SAS_RE = re.compile(r"\?(?:[A-Za-z0-9_-]+=[^&\s]+&){2,}[^&\s]+")
_SAS_SIG_RE = re.compile(r"(?i)([?&])sig=[^&\s\"']+")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}")
_AZURE_KEY_RE = re.compile(
    r"(?i)(account[-_]?key|access[-_]?key|client[-_]?secret)[\"'\s:=]+[A-Za-z0-9+/=_-]{20,}"
)
_GUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Base64 blobs (≥40 chars, common in Azure keys/tokens). The lookahead
# ``(?=[A-Za-z0-9+/]*[0-9+/])`` requires at least one digit or ``+``/``/`` in
# the run, so long DNA / protein FASTA sequences (pure A-Z letters, no digits)
# are NOT mistaken for secrets and collapsed into ``<base64-redacted>``.
# Random base64-encoded keys/tokens essentially always contain digits, so this
# keeps the secret-masking intent intact while leaving biological sequences
# (e.g. a query FASTA shown in the Service Bus playground) untouched.
_BASE64_BLOB_RE = re.compile(
    r"(?<![A-Za-z0-9+/=])(?=[A-Za-z0-9+/]*[0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"
)
# Connection strings (e.g. DefaultEndpointsProtocol=...)
_CONN_STR_RE = re.compile(r"(?i)DefaultEndpointsProtocol=[^\s;]+(?:;[^\s;]+){2,}")
# Password / secret values after common keys
_PASSWORD_RE = re.compile(r"(?i)(password|passwd|pwd|secret|token)[\"'\s:=]+\S{8,}")
# ANSI escape sequences (CSI: ESC [ ... letter). Strip so terminal colour codes
# from elastic-blast / az / azcopy do not leak into JSON state, log artefacts,
# or the dashboard UI.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# Cheap pre-check: avoid the 8-regex pipeline (and 8 fresh string
# allocations) when no plausible secret marker is present. Hits short-
# circuit early; the false-positive rate is acceptable because matching
# this fast regex is O(n) over a SIMD-friendly character class.
_FAST_TRIGGER_RE = re.compile(
    r"(?i)(\?[A-Za-z0-9_-]+=|sig=|bearer\s|account[-_]?key|access[-_]?key|"
    r"client[-_]?secret|DefaultEndpointsProtocol|password|passwd|pwd|secret|token|"
    r"[A-Za-z0-9+/]{40})"
)
_GUID_FAST_RE = re.compile(r"[0-9a-fA-F]{8}-")


def sanitise(text: str | None, *, mask_subscription_ids: bool = True) -> str:
    """Mask sensitive substrings in `text`.

    - SAS query strings → `?<sas-redacted>`
    - Bearer tokens     → `Bearer <redacted>`
    - account/access keys, client secrets → `<key>=<redacted>`
    - Azure GUIDs       → first 8 chars + `…` (subscriptions, tenant ids)
                          when `mask_subscription_ids=True`.

    Short-circuits via ``_FAST_TRIGGER_RE`` / ``_GUID_FAST_RE`` so plain
    text payloads (the common case for streaming log lines) skip the
    full 8-regex pipeline.
    """
    if not text:
        return ""
    out = text
    if "\x1b" in out:
        out = _ANSI_CSI_RE.sub("", out)
    needs_secret_pass = bool(_FAST_TRIGGER_RE.search(out))
    needs_guid_pass = mask_subscription_ids and bool(_GUID_FAST_RE.search(out))
    if not needs_secret_pass and not needs_guid_pass:
        return out
    if needs_secret_pass:
        out = _SAS_RE.sub("?<sas-redacted>", out)
        out = _SAS_SIG_RE.sub(r"\1sig=<redacted>", out)
        out = _BEARER_RE.sub("Bearer <redacted>", out)
        out = _AZURE_KEY_RE.sub(r"\1=<redacted>", out)
        out = _CONN_STR_RE.sub("<connection-string-redacted>", out)
        out = _PASSWORD_RE.sub(r"\1=<redacted>", out)
        out = _BASE64_BLOB_RE.sub("<base64-redacted>", out)
    if needs_guid_pass:
        out = _GUID_RE.sub(_redact_guid, out)
    return out


def _redact_guid(match: re.Match[str]) -> str:
    """GUID abbreviator — first 8 hex chars + ellipsis.

    Factored out of the previous inline lambda so ``re.sub`` keeps a
    stable reference and so the hot path avoids the per-call lambda
    construction cost that ``timeit`` confirmed was measurable on long
    text payloads.
    """
    return match.group(0)[:8] + "…"


def redact_oid(value: str | None) -> str | None:
    """Return a short, non-reversible identifier for a caller GUID.

    Use this whenever ``caller.object_id`` (or any other PII GUID) needs to
    appear in a log line, audit record, or App Insights custom dimension.
    The output is a 12-char sha256 prefix — long enough to disambiguate a
    handful of dashboard users in correlation queries, short enough that it
    cannot be reversed to a GUID by brute force, and free of any GUID
    substring that operators could mistake for the real identifier.

    Returns ``None`` for falsy input so callers can pass it straight into
    structured log fields without an ``if`` guard. The existing terminal
    WebSocket route uses the same primitive (see
    `api/routes/terminal/ws.py::_log_identity_hash`) — that helper is now a
    re-export of this one.
    """
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
