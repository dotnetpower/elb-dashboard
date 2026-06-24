"""On-the-fly result-file decompression and format transcoding for downloads.

Module summary: turns a single stored BLAST result file (XML `outfmt 5` or
tabular `outfmt 6`/`7`, optionally gzip-compressed) into the format a download
caller asked for via the `?decompress=` / `?format=` query options on
`GET /api/v1/elastic-blast/jobs/{job_id}/files/{file_id}`. NCBI Web BLAST lets a
user pick the *format* of one result (Hit Table / CSV / JSON / XML) rather than
toggling compression; this is the dashboard's gateway equivalent — compression
is a transport concern handled here, and format is a re-render of the same
parsed hits using the existing `results_parser`.

Responsibility: pure, streaming/bounded decompression + hit-format conversion.
Edit boundaries: no FastAPI / Storage / Azure SDK imports — only `results_parser`
  and stdlib. HTTP validation + response shaping stay in the route
  (`api/routes/elastic_blast.py`); SAS-free streaming stays in
  `api/services/external_blast.py`.
Key entry points: `is_gzip_name`, `gunzip_stream`, `gunzip_bytes`,
  `transcode_result_bytes`, `strip_gzip_suffix`.
Risky contracts: `gunzip_stream`/`gunzip_bytes` are memory-bounded (a gzip bomb
  wastes bandwidth, never RAM); `transcode_result_bytes` caps its input at
  `TRANSCODE_MAX_BYTES` and raises `ResultTooLargeError` / `ResultParseError`
  (never a bare 500) so the route can surface the reason in the response body.
Validation: `uv run pytest -q api/tests/test_result_transcode.py`.
"""

from __future__ import annotations

import io
import json
import zlib
from collections.abc import Iterator
from typing import Any

from api.services.blast.results_parser import (
    EXPORT_DEFAULT_COLUMNS,
    EXPORT_EXTRA_COLUMNS,
    parse_blast_result_content,
)

# Upper bound for the *decompressed* bytes a transcode will parse. Tabular/XML
# result files the dashboard parses elsewhere are capped at 10 MiB
# (``RESULTS_EXPORT_MAX_BYTES``); keep a little headroom here for a single
# merged shard file while still refusing to buffer an unbounded payload for the
# CPU-heavy parse step. Decompress-only streaming is NOT bound by this — it is
# memory-safe regardless of size.
TRANSCODE_MAX_BYTES = 16 * 1024 * 1024

# Per-step decompression budget so a gzip bomb can never materialise more than
# one step in memory before the running-total cap is checked.
_GUNZIP_STEP_BYTES = 1024 * 1024

# gzip (RFC 1952) wbits for zlib: 16 selects the gzip header/trailer.
_GZIP_WBITS = zlib.MAX_WBITS | 16

# Formats a caller may request via ``?format=``. XML/raw passthrough is the
# default (no ``format``) so this set is only the re-rendered tabular/JSON ones.
SUPPORTED_TARGET_FORMATS = frozenset({"csv", "tsv", "json"})


class ResultTranscodeError(ValueError):
    """Base class for a recoverable transcode failure surfaced in the body."""


class ResultTooLargeError(ResultTranscodeError):
    """The decompressed result exceeds ``TRANSCODE_MAX_BYTES``."""


class ResultParseError(ResultTranscodeError):
    """The bytes could not be parsed as BLAST XML or tabular output."""


def is_gzip_name(filename: str) -> bool:
    """True when the filename signals gzip content (``*.gz``)."""
    return filename.lower().endswith(".gz")


def strip_gzip_suffix(filename: str) -> str:
    """Drop a trailing ``.gz`` so a decompressed download gets a sane name."""
    return filename[:-3] if is_gzip_name(filename) else filename


def result_media_type_for(filename: str) -> str:
    """Content type for a decompressed result filename (XML / tabular / text)."""
    lowered = filename.lower()
    if lowered.endswith(".xml"):
        return "application/xml"
    if lowered.endswith((".out", ".tsv", ".txt", ".log")):
        return "text/plain"
    if lowered.endswith(".csv"):
        return "text/csv"
    if lowered.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


def gunzip_stream(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Streaming gzip → plain bytes. Memory stays at one inflated chunk.

    Used by the decompress-only path so a large result is decompressed on the
    fly without buffering the whole file. A truncated/corrupt stream surfaces as
    a ``zlib.error`` from the iterator (the route closes the response).
    """
    decompressor = zlib.decompressobj(_GZIP_WBITS)
    for chunk in chunks:
        part = decompressor.decompress(chunk)
        if part:
            yield part
    tail = decompressor.flush()
    if tail:
        yield tail


def gunzip_bytes(raw: bytes, *, max_output: int = TRANSCODE_MAX_BYTES) -> bytes:
    """Decompress gzip bytes with a hard cap on the *output* size.

    Bounded by feeding the decompressor in ``_GUNZIP_STEP_BYTES`` slices and
    checking the running total after each, so a gzip bomb raises
    ``ResultTooLargeError`` long before it can exhaust memory. Raises
    ``ResultParseError`` for malformed gzip input.
    """
    decompressor = zlib.decompressobj(_GZIP_WBITS)
    out = bytearray()
    try:
        produced = decompressor.decompress(raw, _GUNZIP_STEP_BYTES)
        while produced:
            out += produced
            if len(out) > max_output:
                raise ResultTooLargeError(
                    "decompressed result exceeds the transcode size limit"
                )
            produced = decompressor.decompress(
                decompressor.unconsumed_tail, _GUNZIP_STEP_BYTES
            )
        out += decompressor.flush()
    except zlib.error as exc:
        raise ResultParseError("result is not valid gzip content") from exc
    if len(out) > max_output:
        raise ResultTooLargeError("decompressed result exceeds the transcode size limit")
    return bytes(out)


def transcode_result_bytes(
    raw: bytes,
    *,
    source_filename: str,
    target_format: str,
) -> tuple[bytes, str, str]:
    """Re-render parsed BLAST hits into ``csv`` / ``tsv`` / ``json``.

    ``raw`` must already be decompressed plaintext. Returns
    ``(body, media_type, filename)``. Raises ``ResultParseError`` when the bytes
    are neither parseable BLAST XML nor tabular output, and ``ResultTooLargeError``
    when ``raw`` is over ``TRANSCODE_MAX_BYTES`` — both carry a human-readable
    message the route puts in the response body.
    """
    if target_format not in SUPPORTED_TARGET_FORMATS:
        raise ResultTranscodeError(f"unsupported target format: {target_format!r}")
    if len(raw) > TRANSCODE_MAX_BYTES:
        raise ResultTooLargeError("result exceeds the transcode size limit")

    text = raw.decode("utf-8", errors="replace")
    try:
        hits = parse_blast_result_content(text)
    except Exception as exc:  # defusedxml / tabular parse failure
        raise ResultParseError(
            "could not parse the result file as BLAST XML or tabular output"
        ) from exc

    stem = _filename_stem(strip_gzip_suffix(source_filename))
    if target_format == "json":
        body = json.dumps(
            {"format": "json", "total": len(hits), "hits": hits}, default=str
        ).encode("utf-8")
        return body, "application/json", f"{stem}.json"

    delimiter = "\t" if target_format == "tsv" else ","
    extras = [col for col in EXPORT_EXTRA_COLUMNS if any(col in hit for hit in hits)]
    columns = list(EXPORT_DEFAULT_COLUMNS) + extras
    body = _render_delimited(columns, delimiter, hits).encode("utf-8")
    media_type = "text/tab-separated-values" if target_format == "tsv" else "text/csv"
    ext = "tsv" if target_format == "tsv" else "csv"
    return body, media_type, f"{stem}.{ext}"


def _render_delimited(
    columns: list[str], delimiter: str, hits: list[dict[str, Any]]
) -> str:
    import csv

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=columns, delimiter=delimiter, extrasaction="ignore"
    )
    writer.writeheader()
    for hit in hits:
        writer.writerow(hit)
    return buf.getvalue()


def _filename_stem(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1]
    for suffix in (".out", ".xml", ".tsv", ".txt", ".csv", ".json"):
        if base.lower().endswith(suffix):
            return base[: -len(suffix)] or "blast_result"
    return base or "blast_result"
