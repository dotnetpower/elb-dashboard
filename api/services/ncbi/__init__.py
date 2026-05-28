"""NCBI E-utilities integration (esummary / efetch) for the dashboard.

Responsibility: Re-export the public NCBI service surface so routes/tasks import
from a single name. Implementation lives in focused submodules (`_eutils`,
`nuccore`).
Edit boundaries: Put new shared HTTP/identity/rate helpers in `_eutils.py`,
record-type-specific parsing (nuccore, protein, gene) in their own modules.
Key entry points: `NcbiServiceUnavailable`, `fetch_nuccore_summary`,
`fetch_nuccore_genbank`, `fetch_nuccore_fasta`, `clear_nuccore_caches`.
Risky contracts: All calls to NCBI go through `_eutils._request_*` so the
per-process token bucket and `_ncbi_identity_params` are applied consistently.
Validation: `uv run pytest -q api/tests/test_ncbi_nuccore.py`.
"""

from __future__ import annotations

from api.services.ncbi._eutils import (
    EUTILS_BASE_URL,
    NcbiRateLimited,
    NcbiResponseTooLarge,
    NcbiServiceUnavailable,
)
from api.services.ncbi.nuccore import (
    MAX_FASTA_BYTES,
    MAX_GENBANK_BYTES,
    MAX_SUMMARY_BYTES,
    clear_nuccore_caches,
    fetch_nuccore_fasta,
    fetch_nuccore_genbank,
    fetch_nuccore_summary,
    normalise_accession,
)

__all__ = [
    "EUTILS_BASE_URL",
    "MAX_FASTA_BYTES",
    "MAX_GENBANK_BYTES",
    "MAX_SUMMARY_BYTES",
    "NcbiRateLimited",
    "NcbiResponseTooLarge",
    "NcbiServiceUnavailable",
    "clear_nuccore_caches",
    "fetch_nuccore_fasta",
    "fetch_nuccore_genbank",
    "fetch_nuccore_summary",
    "normalise_accession",
]
