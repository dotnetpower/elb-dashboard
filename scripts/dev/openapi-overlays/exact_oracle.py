"""Exact-sharding Storage metadata for the elb-openapi build-context overlay.

Responsibility: Validate one active database generation and its matching
DB-order oracle, then write the bounded oracle manifest into private job results.
Edit boundaries: This file is copied verbatim into the sibling docker-openapi
``app/`` directory; keep it independent of dashboard packages and browser APIs.
Key entry points: ``attach_db_order_oracle``, ``ensure_tabular_raw_score``,
``read_active_database``, ``set_search_space``.
Risky contracts: OAuth tokens never leave request headers, every Storage request
has a timeout, immutable DB/shard paths must match the generation ID, all oracle
parts must exist and be non-empty, and no SAS URL is created or returned.
Validation: ``uv run pytest -q scripts/dev/openapi-overlays/test_exact_oracle.py``.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import requests

_STORAGE_API_VERSION = "2020-04-08"
_REQUEST_TIMEOUT = (3.05, 15)
_DB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_SHARD_RE = re.compile(r"^[0-9]{2}$")
_MAX_PARTS = 1024
_CALIBRATION_QUERY_LEN = 64
_CALIBRATION_LENGTH_ADJUSTMENT = 33


class ExactOracleUnavailable(RuntimeError):
    """Raised without credentials or Storage response bodies in its message."""


@dataclass(frozen=True, slots=True)
class AttachedOracle:
    run_id: str
    source_version: str
    part_count: int
    manifest_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_version": self.source_version,
            "part_count": self.part_count,
        }


@dataclass(frozen=True, slots=True)
class ActiveDatabase:
    source_version: str
    db_prefix: str
    shard_layout_prefix: str
    total_letters: int
    total_sequences: int
    search_space: int


def _headers(token: str) -> dict[str, str]:
    if not token:
        raise ExactOracleUnavailable("Storage OAuth token is unavailable")
    return {
        "Authorization": f"Bearer {token}",
        "x-ms-version": _STORAGE_API_VERSION,
        "x-ms-date": datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT"),
    }


def _validate_urls(blob_base: str, results_url: str) -> tuple[str, str]:
    base = blob_base.rstrip("/")
    results = results_url.rstrip("/")
    parsed = urlparse(base)
    result_parsed = urlparse(results)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.query
        or result_parsed.scheme != "https"
        or result_parsed.hostname != parsed.hostname
        or result_parsed.query
        or not results.startswith(f"{base}/results/")
    ):
        raise ExactOracleUnavailable("Storage results URL is outside the configured account")
    return base, results


def _validate_blob_base(blob_base: str) -> str:
    base = blob_base.rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query:
        raise ExactOracleUnavailable("Storage blob base URL is invalid")
    return base


def _request_ok(response: Any, operation: str) -> None:
    if int(getattr(response, "status_code", 0) or 0) not in {200, 201}:
        raise ExactOracleUnavailable(f"Storage {operation} did not succeed")


def attach_db_order_oracle(
    *,
    blob_base: str,
    results_url: str,
    db_name: str,
    expected_source_version: str,
    token: str,
) -> AttachedOracle:
    """Validate and attach a complete current DB-order oracle to one job."""
    if not _DB_RE.fullmatch(db_name):
        raise ExactOracleUnavailable("Database name is invalid for exact oracle lookup")
    source_version = expected_source_version.strip()
    if not source_version or source_version.lower() == "unknown":
        raise ExactOracleUnavailable("Database source version is unavailable")
    base, results = _validate_urls(blob_base, results_url)
    headers = _headers(token)
    status_url = f"{base}/blast-db/metadata/oracles/{db_name}/status.json"
    response = requests.get(status_url, headers=headers, timeout=_REQUEST_TIMEOUT)
    _request_ok(response, "oracle status read")
    try:
        status = response.json()
    except (TypeError, ValueError) as exc:
        raise ExactOracleUnavailable("Oracle status is not valid JSON") from exc
    if not isinstance(status, dict) or status.get("status") != "ready":
        raise ExactOracleUnavailable("DB-order oracle is not ready")
    run_id = str(status.get("run_id") or "")
    oracle_source = str(status.get("source_version") or "")
    shards = status.get("expected_shards")
    expected_parts = int(status.get("expected_parts") or 0)
    ready_parts = int(status.get("ready_parts") or 0)
    if (
        not _RUN_RE.fullmatch(run_id)
        or oracle_source != source_version
        or not isinstance(shards, list)
        or not 0 < expected_parts <= _MAX_PARTS
        or ready_parts != expected_parts
        or len(shards) != expected_parts
        or len(set(str(shard) for shard in shards)) != expected_parts
        or any(not _SHARD_RE.fullmatch(str(shard)) for shard in shards)
    ):
        raise ExactOracleUnavailable("DB-order oracle does not match the database generation")

    part_urls = [
        f"{base}/blast-db/metadata/oracles/{db_name}/parts/{run_id}/{shard}.txt" for shard in shards
    ]
    for part_url in part_urls:
        part = requests.head(part_url, headers=headers, timeout=_REQUEST_TIMEOUT)
        _request_ok(part, "oracle part validation")
        try:
            size = int(part.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            raise ExactOracleUnavailable("DB-order oracle contains an empty part")

    manifest_url = f"{results}/metadata/tie-order-oracle-urls.txt"
    manifest = ("\n".join(part_urls) + "\n").encode("utf-8")
    put_headers = {
        **headers,
        "Content-Type": "text/plain; charset=utf-8",
        "x-ms-blob-type": "BlockBlob",
    }
    uploaded = requests.put(
        manifest_url,
        headers=put_headers,
        data=manifest,
        timeout=_REQUEST_TIMEOUT,
    )
    _request_ok(uploaded, "oracle manifest write")
    return AttachedOracle(
        run_id=run_id,
        source_version=source_version,
        part_count=expected_parts,
        manifest_url=manifest_url,
    )


def read_active_database(
    *,
    blob_base: str,
    db_name: str,
    token: str,
) -> ActiveDatabase:
    """Read active generation identity and calibrated counts from Storage."""
    if not _DB_RE.fullmatch(db_name):
        raise ExactOracleUnavailable("Database name is invalid for metadata lookup")
    base = _validate_blob_base(blob_base)
    metadata_url = f"{base}/blast-db/{db_name}-metadata.json"
    response = requests.get(
        metadata_url,
        headers=_headers(token),
        timeout=_REQUEST_TIMEOUT,
    )
    _request_ok(response, "active database metadata read")
    try:
        metadata = response.json()
    except (TypeError, ValueError) as exc:
        raise ExactOracleUnavailable("Active database metadata is not valid JSON") from exc
    if not isinstance(metadata, dict):
        raise ExactOracleUnavailable("Active database metadata is invalid")
    active = metadata.get("active_generation")
    active_id = active.get("id") if isinstance(active, dict) else None
    source_version = str(active_id or metadata.get("source_version") or "").strip()
    db_prefix = str(
        metadata.get("active_prefix")
        or (active.get("prefix") if isinstance(active, dict) else "")
        or ""
    ).strip("/")
    shard_layout_prefix = str(metadata.get("shard_layout_prefix") or "").strip("/")
    try:
        total_letters = int(
            metadata.get("total_letters")
            or metadata.get("number_of_letters")
            or metadata.get("number-of-letters")
            or 0
        )
        total_sequences = int(
            metadata.get("total_sequences")
            or metadata.get("number_of_sequences")
            or metadata.get("number-of-sequences")
            or 0
        )
    except (TypeError, ValueError) as exc:
        raise ExactOracleUnavailable("Active database statistics are invalid") from exc
    detail = {
        "detail": {
            "number_of_letters": total_letters,
            "number_of_sequences": total_sequences,
        }
    }
    search_space = search_space_from_db_version(detail)
    if not source_version:
        raise ExactOracleUnavailable("Active database generation is unavailable")
    expected_db_prefix = f"{db_name}/generations/{source_version}/{db_name}"
    expected_shard_prefix = f"{db_name}/generations/{source_version}/shards"
    if db_prefix != expected_db_prefix or shard_layout_prefix != expected_shard_prefix:
        raise ExactOracleUnavailable("Active database generation paths are invalid")
    return ActiveDatabase(
        source_version=source_version,
        db_prefix=db_prefix,
        shard_layout_prefix=shard_layout_prefix,
        total_letters=total_letters,
        total_sequences=total_sequences,
        search_space=search_space,
    )


def ensure_tabular_raw_score(options: str) -> str:
    """Append ``score`` to a tabular outfmt while preserving all other flags."""
    try:
        tokens = shlex.split(options or "")
    except ValueError as exc:
        raise ExactOracleUnavailable("BLAST options cannot be parsed") from exc
    start = None
    end = None
    spec: list[str] = []
    for index, argument in enumerate(tokens):
        if argument == "-outfmt" and index + 1 < len(tokens):
            start = index
            end = index + 1
            while end < len(tokens) and not tokens[end].startswith("-"):
                spec.extend(tokens[end].split())
                end += 1
            break
        if argument.startswith("-outfmt="):
            start = index
            end = index + 1
            spec = argument.split("=", 1)[1].split()
            break
    if start is None:
        return " ".join([*tokens, "-outfmt", "6 std score"])
    if not spec or spec[0] not in {"6", "7"}:
        return options
    fields = spec[1:] or ["std"]
    expanded = set(fields)
    if "std" in fields:
        expanded.update(
            {
                "qseqid",
                "sseqid",
                "pident",
                "length",
                "mismatch",
                "gapopen",
                "qstart",
                "qend",
                "sstart",
                "send",
                "evalue",
                "bitscore",
            }
        )
    if "score" not in expanded:
        fields.append("score")
    replacement = ["-outfmt", " ".join([spec[0], *fields])]
    return " ".join([*tokens[:start], *replacement, *tokens[end:]])


def search_space_from_db_version(db_version: Any) -> int:
    """Derive calibrated search space from sibling DB metadata details."""
    detail = db_version.get("detail") if isinstance(db_version, dict) else None
    if not isinstance(detail, dict):
        raise ExactOracleUnavailable("Active database statistics are unavailable")
    try:
        db_num = int(detail.get("number_of_sequences") or 0)
        db_len = int(detail.get("number_of_letters") or 0)
    except (TypeError, ValueError) as exc:
        raise ExactOracleUnavailable("Active database statistics are invalid") from exc
    effective_query = _CALIBRATION_QUERY_LEN - _CALIBRATION_LENGTH_ADJUSTMENT
    effective_db = db_len - db_num * _CALIBRATION_LENGTH_ADJUSTMENT
    value = effective_query * effective_db
    if db_num <= 0 or db_len <= 0 or effective_query <= 0 or effective_db <= 0:
        raise ExactOracleUnavailable("Active database statistics cannot be calibrated")
    return value


def set_search_space(options: str, value: int) -> str:
    """Replace all scalar ``-searchsp`` forms with the active value."""
    if value <= 0:
        raise ExactOracleUnavailable("Search space must be positive")
    try:
        tokens = shlex.split(options or "")
    except ValueError as exc:
        raise ExactOracleUnavailable("BLAST options cannot be parsed") from exc
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        argument = tokens[index]
        if argument in {"-searchsp", "-dbsize"}:
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
                raise ExactOracleUnavailable(f"{argument} requires a scalar value")
            index += 2
            continue
        if argument.startswith(("-searchsp=", "-dbsize=")):
            index += 1
            continue
        kept.append(argument)
        index += 1
    return shlex.join([*kept, "-searchsp", str(value)])
