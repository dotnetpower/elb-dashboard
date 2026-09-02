"""Resolve precise BLAST search space from the active database generation.

Responsibility: Read trusted workload database counts and derive the canonical
Web BLAST calibration value used by non-browser submit surfaces.
Edit boundaries: Keep Storage metadata lookup and scalar option rewriting here;
submit routes/tasks own HTTP, queue, and state behavior.
Key entry points: `resolve_live_search_space`, `canonicalize_precise_options`,
`set_search_space_option`.
Risky contracts: Precise calibrated databases fail closed when active metadata
is unavailable; caller-provided stale values never override the active
snapshot; no URL, token, or Storage response is returned to callers.
Validation: `uv run pytest -q api/tests/test_live_search_space.py`.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class LiveSearchSpaceUnavailable(RuntimeError):
    """Raised when a precise calibrated submit lacks active DB statistics."""


@dataclass(frozen=True, slots=True)
class LiveSearchSpace:
    database: str
    source_version: str
    total_letters: int
    total_sequences: int
    value: int


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _metadata_count(metadata: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _positive_int(metadata.get(key))
        if value is not None:
            return value
    return None


def resolve_live_search_space(
    database: str,
    *,
    storage_account: str | None = None,
) -> LiveSearchSpace | None:
    """Return active calibrated DB statistics, or ``None`` for uncalibrated DBs."""
    from api.services.blast.db_metadata import extract_db_name, resolve_db_metadata
    from api.services.web_blast_searchsp import (
        compute_web_blast_searchsp,
        default_for_database,
    )

    db_name = extract_db_name(database) or database.strip()
    if default_for_database(db_name) is None:
        return None
    account = (
        (storage_account or "").strip()
        or os.environ.get("STORAGE_ACCOUNT_NAME", "").strip()
        or os.environ.get("AZURE_STORAGE_ACCOUNT", "").strip()
    )
    if not account:
        raise LiveSearchSpaceUnavailable("workload Storage account is unavailable")
    metadata = resolve_db_metadata(account, db_name)
    if not isinstance(metadata, Mapping):
        raise LiveSearchSpaceUnavailable("active database metadata is unavailable")
    total_letters = _metadata_count(
        metadata,
        "total_letters",
        "number_of_letters",
        "number-of-letters",
    )
    total_sequences = _metadata_count(
        metadata,
        "total_sequences",
        "number_of_sequences",
        "number-of-sequences",
    )
    if total_letters is None or total_sequences is None:
        raise LiveSearchSpaceUnavailable("active database counts are unavailable")
    value = compute_web_blast_searchsp(total_letters, total_sequences)
    if value is None:
        raise LiveSearchSpaceUnavailable("active database counts cannot be calibrated")
    active_generation = metadata.get("active_generation")
    active_generation_id = (
        active_generation.get("id") if isinstance(active_generation, Mapping) else None
    )
    source_version = str(
        active_generation_id or metadata.get("source_version") or metadata.get("version") or ""
    ).strip()
    return LiveSearchSpace(
        database=db_name,
        source_version=source_version,
        total_letters=total_letters,
        total_sequences=total_sequences,
        value=value,
    )


def canonicalize_precise_options(
    database: str,
    options: Mapping[str, Any] | None,
    *,
    storage_account: str | None = None,
) -> dict[str, Any]:
    """Overwrite precise calibrated options with active generation statistics."""
    from api.services.sharding_precision import normalize_sharding_mode
    from api.services.web_blast_searchsp import default_for_database

    resolved = dict(options or {})
    if normalize_sharding_mode(resolved) != "precise":
        return resolved
    if default_for_database(database) is None:
        return resolved
    account = (
        (storage_account or "").strip()
        or os.environ.get("STORAGE_ACCOUNT_NAME", "").strip()
        or os.environ.get("AZURE_STORAGE_ACCOUNT", "").strip()
    )
    if not account:
        return resolved
    live = resolve_live_search_space(database, storage_account=account)
    if live is None:  # guarded by calibrated-database check; survive ``-O``
        raise LiveSearchSpaceUnavailable("calibrated database metadata is unavailable")
    resolved["db_total_letters"] = live.total_letters
    resolved["db_total_sequences"] = live.total_sequences
    resolved["db_effective_search_space"] = live.value
    additional = str(resolved.get("additional_options") or "").strip()
    if additional:
        resolved["additional_options"] = set_search_space_option(additional, live.value)
    return resolved


def set_search_space_option(options: str, value: int) -> str:
    """Replace all scalar ``-searchsp`` forms with one canonical trailing value."""
    if value <= 0:
        raise ValueError("search space must be positive")
    try:
        tokens = shlex.split(options or "")
    except ValueError as exc:
        raise ValueError(f"invalid BLAST options: {exc}") from exc
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        argument = tokens[index]
        if argument in {"-searchsp", "-dbsize"}:
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
                raise ValueError(f"{argument} requires a scalar value")
            index += 2
            continue
        if argument.startswith(("-searchsp=", "-dbsize=")):
            index += 1
            continue
        kept.append(argument)
        index += 1
    return shlex.join([*kept, "-searchsp", str(value)])
