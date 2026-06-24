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
import os
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
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

# Hard ceiling on the *output* of the streaming decompress path (``?decompress``).
# Memory is already bounded (one inflated step at a time), but without an output
# ceiling a tiny gzip bomb could stream unbounded bytes to a token-only caller.
# Generous (real merged BLAST results are well under this) and env-tunable so an
# operator can lower it without a redeploy. Exceeding it terminates the stream.
_DECOMPRESS_STREAM_MAX_ENV = "DOWNLOAD_DECOMPRESS_MAX_BYTES"
_DEFAULT_DECOMPRESS_STREAM_MAX = 2 * 1024 * 1024 * 1024  # 2 GiB

# gzip (RFC 1952) wbits for zlib: 16 selects the gzip header/trailer.
_GZIP_WBITS = zlib.MAX_WBITS | 16

# gzip member magic (RFC 1952 §2.3.1). Used to sniff content whose filename /
# upstream media type did not advertise gzip, so a mislabeled result is still
# handled correctly instead of being decoded as garbage.
_GZIP_MAGIC = b"\x1f\x8b"

# Window of bytes inspected for a NUL byte to reject binary content before the
# lenient tabular parser turns it into a misleading zero-hit (header-only) file.
_BINARY_SNIFF_BYTES = 64 * 1024

# Formats a caller may request via ``?format=``. XML/raw passthrough is the
# default (no ``format``) so this set is only the re-rendered tabular/JSON ones.
SUPPORTED_TARGET_FORMATS = frozenset({"csv", "tsv", "json"})


class ResultTranscodeError(ValueError):
    """Base class for a recoverable transcode failure surfaced in the body."""


class ResultTooLargeError(ResultTranscodeError):
    """The decompressed result exceeds ``TRANSCODE_MAX_BYTES``."""


class ResultParseError(ResultTranscodeError):
    """The bytes could not be parsed as BLAST XML or tabular output."""


class TransformBusyError(Exception):
    """Too many concurrent in-memory transforms — caller should retry (503).

    Deliberately NOT a ``ResultTranscodeError`` so the route maps it to 503
    (transient), never 422 (permanent parse failure).
    """


# Bound the number of concurrent ``?format=`` transforms. Each buffers the file
# (capped at ``TRANSCODE_MAX_BYTES``) AND holds the parsed hits + rendered body
# in memory at once, so unbounded concurrency on the token-authorised (no-RBAC)
# download route is a memory-exhaustion vector. The streaming ``?decompress``
# path is memory-bounded and intentionally NOT gated here. Env-tunable; mirrors
# the §9 data-plane transfer cap of 4.
_TRANSFORM_CONCURRENCY_ENV = "DOWNLOAD_TRANSFORM_CONCURRENCY"
_DEFAULT_TRANSFORM_CONCURRENCY = 4
_TRANSFORM_ACQUIRE_TIMEOUT_SEC = 30.0


def _resolve_transform_concurrency() -> int:
    raw = os.environ.get(_TRANSFORM_CONCURRENCY_ENV, "").strip()
    if not raw:
        return _DEFAULT_TRANSFORM_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TRANSFORM_CONCURRENCY
    return value if value > 0 else _DEFAULT_TRANSFORM_CONCURRENCY


_transform_semaphore = threading.BoundedSemaphore(_resolve_transform_concurrency())


@contextmanager
def transform_slot(*, timeout: float | None = None) -> Iterator[None]:
    """Acquire a bounded transform slot or raise ``TransformBusyError``.

    Wrap the buffer+parse+render block so peak memory is bounded by
    ``concurrency × per-transform`` instead of the unbounded request fan-in.
    """
    wait = _TRANSFORM_ACQUIRE_TIMEOUT_SEC if timeout is None else timeout
    if not _transform_semaphore.acquire(timeout=wait):
        raise TransformBusyError("too many concurrent result transforms; retry shortly")
    try:
        yield
    finally:
        _transform_semaphore.release()


def is_gzip_name(filename: str) -> bool:
    """True when the filename signals gzip content (``*.gz``)."""
    return filename.lower().endswith(".gz")


def looks_like_gzip(raw: bytes) -> bool:
    """True when the bytes start with the gzip member magic (RFC 1952).

    Lets the route decompress a result whose filename / upstream media type did
    not advertise gzip, instead of decoding the compressed bytes as garbage.
    """
    return raw[:2] == _GZIP_MAGIC


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
    fly without buffering the whole file. If the first non-empty chunk does not
    carry the gzip magic the bytes are streamed **unchanged** (a mislabeled
    ``.gz`` file is passed through instead of truncating with a ``zlib.error``).
    The total output is bounded by ``_decompress_stream_max()`` so a gzip bomb
    cannot stream unbounded bytes — exceeding it raises ``ResultTooLargeError``
    (which terminates the already-started response rather than running forever).
    """
    decompressor = zlib.decompressobj(_GZIP_WBITS)
    limit = _decompress_stream_max()
    produced_total = 0
    decided = False
    passthrough = False
    for chunk in chunks:
        if not decided and chunk:
            decided = True
            passthrough = chunk[:2] != _GZIP_MAGIC
        if passthrough:
            if chunk:
                yield chunk
            continue
        part = decompressor.decompress(chunk)
        if part:
            produced_total += len(part)
            if produced_total > limit:
                raise ResultTooLargeError(
                    "decompressed stream exceeds the decompress size limit"
                )
            yield part
    if not passthrough:
        tail = decompressor.flush()
        if tail:
            yield tail


def _decompress_stream_max() -> int:
    raw = os.environ.get(_DECOMPRESS_STREAM_MAX_ENV, "").strip()
    if not raw:
        return _DEFAULT_DECOMPRESS_STREAM_MAX
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_DECOMPRESS_STREAM_MAX
    return value if value > 0 else _DEFAULT_DECOMPRESS_STREAM_MAX


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

    # Reject content that is clearly not BLAST text BEFORE the lenient tabular
    # parser silently yields zero rows and we emit a misleading header-only
    # file. Two cheap binary signals: a leftover gzip member (the route failed
    # to decompress it) or a NUL byte (BLAST XML/tabular is NUL-free text).
    if looks_like_gzip(raw):
        raise ResultParseError(
            "result is still gzip-compressed; cannot re-render — "
            "download without ?format or with ?decompress=1"
        )
    if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
        raise ResultParseError("result is binary, not BLAST XML or tabular output")

    text = raw.decode("utf-8", errors="replace")
    try:
        hits = parse_blast_result_content(text)
    except Exception as exc:  # defusedxml / tabular parse failure
        raise ResultParseError(
            "could not parse the result file as BLAST XML or tabular output"
        ) from exc

    # The tabular parser tolerates junk by skipping unrecognised lines, so a
    # non-BLAST text file parses to zero hits. Distinguish a legitimately
    # empty/zero-hit result (emit the header-only file — correct) from genuine
    # garbage (fail loudly so the consumer is not handed a deceptive empty file).
    if not hits and text.strip() and not _looks_like_blast_text(text):
        raise ResultParseError(
            "result does not look like BLAST XML or tabular output"
        )

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


def _looks_like_blast_text(text: str) -> bool:
    """Heuristic: does this text plausibly hold BLAST XML or tabular output?

    Used only to decide whether a zero-hit parse is a legitimate empty result
    (header-only file is correct) or genuine garbage (fail loudly). True for
    BLAST XML, an ``outfmt 7`` comment block, or an ``outfmt 6`` data row (>= 12
    tab-separated fields). The first non-blank line decides — a leading
    non-comment, non-tabular line is treated as non-BLAST.
    """
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith(("<?xml", "<BlastOutput")):
        return True
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if candidate.startswith("#"):
            return True
        return "\t" in candidate and len(candidate.split("\t")) >= 12
    return False
