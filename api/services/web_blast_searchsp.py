"""Verified Web BLAST-compatible search-space defaults.

The values here are intentionally small in scope: each entry is a measured
baseline for a specific database snapshot and BLAST option set. Callers may
override the injected value with an explicit ``-searchsp`` in
``additional_options``.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class WebBlastSearchSpaceDefault:
    db_name: str
    value: int
    scope: str
    evidence: str
    blast_version: str
    database_snapshot: str
    option_scope: str
    revalidate_when: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "db_name": self.db_name,
            "value": self.value,
            "scope": self.scope,
            "evidence": self.evidence,
            "blast_version": self.blast_version,
            "database_snapshot": self.database_snapshot,
            "option_scope": self.option_scope,
            "revalidate_when": self.revalidate_when,
        }


WEB_BLAST_SEARCHSP_DEFAULTS: dict[str, WebBlastSearchSpaceDefault] = {
    "core_nt": WebBlastSearchSpaceDefault(
        db_name="core_nt",
        value=32_156_241_807_668,
        scope=(
            "blastn, core_nt 2026-05-09 snapshot, 64 nt calibration query, "
            "word_size=28, dust=yes, evalue=10, max_target_seqs=500, outfmt=5"
        ),
        evidence="docs/temp/core-nt-searchsp/core_nt-searchsp-calibration-results.tgz",
        blast_version="BLASTN 2.17.0+",
        database_snapshot=(
            "core_nt 2026-05-09; BLASTDB v5; 125,619,662 sequences; "
            "1,041,443,571,674 bases"
        ),
        option_scope="word_size=28, dust=yes, evalue=10, max_target_seqs=500, outfmt=5",
        revalidate_when=(
            "Recalibrate when BLAST+ version, database snapshot, query class, "
            "or option profile changes."
        ),
    ),
}


def database_name_from_path(database: str) -> str:
    """Return the bare database name from a dashboard DB path or URL."""
    raw = (database or "").strip()
    if not raw:
        return ""
    if raw.startswith("https://"):
        parsed = urlparse(raw)
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "blast-db":
            return parts[-1] if parts[-1] else parts[1]
        return parts[-1] if parts else ""
    raw = raw.removeprefix("blast-db/")
    parts = [part for part in raw.split("/") if part]
    return parts[-1] if parts else ""


def default_for_database(database: str) -> WebBlastSearchSpaceDefault | None:
    """Return the verified search-space default for a DB path/name, if known."""
    return WEB_BLAST_SEARCHSP_DEFAULTS.get(database_name_from_path(database))
