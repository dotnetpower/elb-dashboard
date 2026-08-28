"""Provider-independent BLAST database generation identity and paths.

Responsibility: Canonicalize NCBI release facts into stable generation
    fingerprints and validate generation-scoped Blob prefixes.
Edit boundaries: Pure value validation and hashing only; network discovery,
    Storage reads/writes, task dispatch, and HTTP response shaping stay in
    their owning modules.
Key entry points: `release_fingerprint`, `generation_id`,
    `generation_data_prefix`, `resolve_active_db_prefix`.
Risky contracts: Fingerprints must not include transfer-provider details so an
    FTP release later mirrored to S3 remains the same logical generation;
    returned prefixes are always relative to the `blast-db` container.
Validation: `uv run pytest -q api/tests/test_db_generations.py`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Any

_SAFE_DB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_GENERATION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def canonical_release_facts(db_name: str, release: Mapping[str, Any]) -> dict[str, Any]:
    """Return provider-neutral facts that identify one NCBI DB release."""
    if not _SAFE_DB_RE.fullmatch(db_name):
        raise ValueError(f"invalid db_name: {db_name!r}")
    released_at = str(
        release.get("released_at")
        or release.get("last_updated")
        or release.get("last-updated")
        or ""
    ).strip()
    if not released_at:
        raise ValueError("release timestamp is required")
    return {
        "db_name": db_name,
        "released_at": released_at,
        "number_of_letters": _positive_int(
            release.get("number_of_letters") or release.get("number-of-letters")
        ),
        "number_of_sequences": _positive_int(
            release.get("number_of_sequences") or release.get("number-of-sequences")
        ),
        "number_of_volumes": _positive_int(
            release.get("number_of_volumes") or release.get("number-of-volumes")
        ),
        "bytes_total": _positive_int(release.get("bytes_total") or release.get("bytes-total")),
    }


def release_fingerprint(db_name: str, release: Mapping[str, Any]) -> str:
    """Hash logical release facts, deliberately excluding source provider."""
    encoded = json.dumps(
        canonical_release_facts(db_name, release),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def release_marker_date(value: str | None) -> date | None:
    """Extract an ISO or compact YYYYMMDD date from a generation marker."""
    text = value or ""
    match = re.search(r"(?<!\d)(\d{4})-?(\d{2})-?(\d{2})(?!\d)", text)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def release_is_at_least(current: str | None, candidate: str | None) -> bool:
    """Return whether current is known to be no older than candidate."""
    current_date = release_marker_date(current)
    candidate_date = release_marker_date(candidate)
    return (
        current_date is not None and candidate_date is not None and current_date >= candidate_date
    )


def generation_id(
    provider: str,
    released_at: str,
    fingerprint: str,
) -> str:
    """Build a short, deterministic, Kubernetes-safe generation identifier."""
    provider_slug = re.sub(r"[^a-z0-9]+", "-", provider.lower()).strip("-")
    if provider_slug not in {"s3", "ncbi-direct"}:
        raise ValueError(f"unsupported source provider: {provider!r}")
    date = re.sub(r"[^0-9]", "", released_at)[:8]
    if len(date) != 8 or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("released_at date and SHA-256 fingerprint are required")
    value = f"{provider_slug}-{date}-{fingerprint[:12]}"
    if not _SAFE_GENERATION_RE.fullmatch(value):
        raise ValueError("generated generation id is invalid")
    return value


def generation_data_prefix(db_name: str, generation: str) -> str:
    """Return the generation directory containing extracted DB files."""
    if not _SAFE_DB_RE.fullmatch(db_name):
        raise ValueError(f"invalid db_name: {db_name!r}")
    if not _SAFE_GENERATION_RE.fullmatch(generation):
        raise ValueError(f"invalid generation id: {generation!r}")
    return f"{db_name}/generations/{generation}"


def generation_db_prefix(db_name: str, generation: str) -> str:
    """Return the BLAST basename prefix for a generation."""
    return f"{generation_data_prefix(db_name, generation)}/{db_name}"


def resolve_active_db_prefix(
    db_name: str,
    metadata: Mapping[str, Any] | None,
) -> str:
    """Resolve a validated active basename prefix or the legacy path."""
    if not _SAFE_DB_RE.fullmatch(db_name):
        raise ValueError(f"invalid db_name: {db_name!r}")
    legacy = f"{db_name}/{db_name}"
    if not metadata:
        return legacy
    raw = str(metadata.get("active_prefix") or "").strip("/")
    if not raw:
        return legacy
    expected = re.compile(
        rf"^{re.escape(db_name)}/generations/[a-z0-9][a-z0-9-]{{7,63}}/{re.escape(db_name)}$"
    )
    if not expected.fullmatch(raw):
        raise ValueError(f"invalid active_prefix for {db_name!r}")
    return raw


__all__ = [
    "canonical_release_facts",
    "generation_data_prefix",
    "generation_db_prefix",
    "generation_id",
    "release_fingerprint",
    "release_is_at_least",
    "release_marker_date",
    "resolve_active_db_prefix",
]
