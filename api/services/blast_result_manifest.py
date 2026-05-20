"""Canonical BLAST result manifest helpers.

Responsibility: Canonical BLAST result manifest helpers
Edit boundaries: Keep reusable domain logic here; routes and tasks should call this layer
instead of duplicating SDK code.
Key entry points: `build_result_manifest`, `_manifest_entry`, `_format_from_name`
Risky contracts: Keep Azure credentials centralized and sanitise data before HTTP, WebSocket, or
log boundaries.
Validation: `uv run pytest -q api/tests/test_blast_results_parser.py
api/tests/test_blast_tasks.py`.
"""

from __future__ import annotations

from typing import Any


def build_result_manifest(
    *,
    job_id: str,
    files: list[dict[str, Any]],
    source: str = "storage",
    degraded_reason: str | None = None,
) -> dict[str, Any]:
    entries = [_manifest_entry(file, index) for index, file in enumerate(files)]
    if degraded_reason:
        status = "degraded"
    elif entries:
        status = "available"
    else:
        status = "no_result_files"
    return {
        "schema_version": 1,
        "job_id": job_id,
        "status": status,
        "source": source,
        "degraded_reason": degraded_reason,
        "files": entries,
        "file_count": len(entries),
        "parseable_count": sum(1 for entry in entries if entry["parseable"]),
    }


def _manifest_entry(file: dict[str, Any], index: int) -> dict[str, Any]:
    name = str(file.get("name") or file.get("filename") or file.get("file_id") or f"file-{index}")
    file_id = str(file.get("file_id") or name)
    result_format = str(file.get("format") or _format_from_name(name))
    return {
        "file_id": file_id,
        "name": name,
        "size": file.get("size") if file.get("size") is not None else file.get("size_bytes"),
        "last_modified": file.get("last_modified"),
        "format": result_format,
        "parseable": result_format in {"blast_xml", "blast_tabular", "xml", "tabular"},
        "source": file.get("source") or "result_blob",
    }


def _format_from_name(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith((".xml", ".xml.gz")):
        return "blast_xml"
    if lowered.endswith((".out", ".tsv", ".txt", ".out.gz")):
        return "blast_tabular"
    if lowered.endswith(".json"):
        return "json"
    return "unknown"
