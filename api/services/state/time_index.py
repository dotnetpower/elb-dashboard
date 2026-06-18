"""Time-ordered secondary-index helpers for the ``jobstate`` listing (#50).

Responsibility: Pure key/cursor math for the optional ``jobstateindex`` table so
the genuinely-most-recent N jobs for an owner can be read as a bounded page read
instead of the full-scan-then-sort in ``repository._list_recent_sorted``.
Edit boundaries: Pure functions only — no Azure SDK / Table I/O here. The Table
reads/writes that consume these keys live in ``repository.py`` (it owns the
pooled clients). Keep this module import-light so it stays unit-testable.
Key entry points: ``time_index_enabled``, ``owner_bucket``, ``row_key``,
``encode_cursor``, ``decode_cursor``, ``build_index_entity``.
Risky contracts: The index key is INTENTIONALLY immutable — it is derived only
from ``owner_oid`` and ``created_at``, both of which are set at create time and
never mutated by ``update()``. An index row therefore never moves; the only
index mutations are add-on-create and remove-on-soft-delete. Do not key any part
of the RowKey on a mutable field (status/phase/updated_at) or that invariant
breaks. RowKey is fixed-width zero-padded so lexical (Azure) order == numeric
order, newest first.
Validation: ``uv run pytest -q api/tests/test_jobstate_time_index.py``.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime
from typing import Any

# Table that holds the time-ordered index rows. Created lazily (like the other
# tables) only when the feature is enabled.
INDEX_TABLE_NAME = "jobstateindex"

# Azure Table PartitionKey / RowKey cannot be empty and cannot contain
# ``/ \ # ?``. ``owner_oid`` is an Entra object id (GUID, safe) for
# dashboard-submitted jobs and the empty string for cluster-shared / external
# rows. Map the empty owner to this sentinel bucket so it is a valid, distinct
# PartitionKey. A real ``owner_oid`` can never collide with it (GUIDs do not
# contain ``_`` runs like this and are 36 chars).
SHARED_BUCKET = "__shared__"

# Width of the inverted-ticks prefix. The base below is 10^14 (covers epoch-ms
# timestamps comfortably past the year 2286), so 14 digits is the exact width
# needed for fixed-width zero-padding -> lexical order matches numeric order.
_INVERT_BASE = 10**14
_ROWKEY_WIDTH = 14

_ENABLE_ENV = "JOBSTATE_TIME_INDEX_ENABLED"


def time_index_enabled() -> bool:
    """Return True when the time-ordered index feature is switched on.

    Default OFF (charter §12a Rule 4: new behaviour ships additive /
    default-OFF). When OFF the repository keeps using the legacy full-scan
    path and writes no index rows. Flipping it ON is only safe AFTER a
    completed backfill (an un-backfilled index would under-report old jobs);
    see ``scripts/dev/backfill_jobstate_time_index.py``.
    """
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def owner_bucket(owner_oid: str | None) -> str:
    """Map an ``owner_oid`` to its index PartitionKey.

    Empty / None (cluster-shared or external-origin rows) -> the shared bucket
    sentinel; otherwise the owner_oid verbatim. ``list_for_owner`` reads exactly
    two buckets — the caller's own and the shared one — to reproduce its
    ``owner_oid eq X or owner_oid eq ''`` filter.
    """
    value = (owner_oid or "").strip()
    return value if value else SHARED_BUCKET


def _inverted_ticks(created_at: str | None) -> int:
    """Return ``_INVERT_BASE - epoch_ms(created_at)`` so newer sorts first.

    A missing / unparseable timestamp degrades to ``_INVERT_BASE`` (epoch 0),
    which sorts such rows LAST (oldest) rather than raising — the index must
    never reject a row just because its ``created_at`` is malformed.
    """
    ts = (created_at or "").strip()
    epoch_ms = 0
    if ts:
        try:
            parsed = datetime.fromisoformat(ts)
            epoch_ms = int(parsed.timestamp() * 1000)
        except (ValueError, OverflowError, OSError):
            epoch_ms = 0
    # Clamp into [0, _INVERT_BASE) so the fixed-width format never overflows or
    # goes negative even for an absurd future/just-before-epoch timestamp.
    epoch_ms = max(0, min(epoch_ms, _INVERT_BASE - 1))
    # Invert within [0, _INVERT_BASE - 1] so the result is always exactly
    # ``_ROWKEY_WIDTH`` digits — using ``_INVERT_BASE - epoch_ms`` would yield
    # ``_INVERT_BASE`` (15 digits) for epoch 0 and break fixed-width ordering.
    return (_INVERT_BASE - 1) - epoch_ms


def row_key(created_at: str | None, job_id: str) -> str:
    """Build the index RowKey: ``<inverted_ticks (14 digits)>_<job_id>``.

    Fixed-width zero-padded prefix so Azure's lexical RowKey ordering matches
    numeric ordering (newest first). The ``job_id`` suffix makes the key unique
    and breaks created_at ties deterministically.
    """
    inverted = _inverted_ticks(created_at)
    return f"{inverted:0{_ROWKEY_WIDTH}d}_{job_id}"


def build_index_entity(
    *, job_id: str, owner_oid: str | None, created_at: str | None
) -> dict[str, Any]:
    """Return the Azure Table entity dict for a job's index row."""
    return {
        "PartitionKey": owner_bucket(owner_oid),
        "RowKey": row_key(created_at, job_id),
        "job_id": job_id,
        "created_at": (created_at or "").strip(),
    }


def encode_cursor(last_row_key: str) -> str:
    """Opaque, URL-safe continuation token wrapping the last emitted RowKey."""
    return base64.urlsafe_b64encode(last_row_key.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str | None) -> str:
    """Decode a cursor back to the RowKey, or ``""`` on any malformed input.

    A malformed / tampered cursor degrades gracefully to an empty string (the
    caller then starts from the newest row) rather than raising — an expired or
    garbage cursor must never 500 the list route.
    """
    if not cursor:
        return ""
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    # The RowKey is ``<14 digits>_<job_id>``; reject anything that does not look
    # like one so a tampered token cannot inject an OData fragment downstream
    # (the repository still OData-escapes it, but fail closed here too).
    prefix, _, _suffix = decoded.partition("_")
    if len(prefix) != _ROWKEY_WIDTH or not prefix.isdigit():
        return ""
    return decoded
