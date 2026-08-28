"""Tests for provider-independent BLAST database generation identity.

Responsibility: Lock canonical release fingerprints and safe generation path
    validation without exercising network or Storage clients.
Edit boundaries: Pure generation-domain behavior only.
Key entry points: Tests for `api.services.db.generations`.
Risky contracts: Provider changes must not change a logical release fingerprint
    and legacy metadata must continue resolving to `<db>/<db>`.
Validation: `uv run pytest -q api/tests/test_db_generations.py`.
"""

import pytest
from api.services.db.generations import (
    generation_db_prefix,
    generation_id,
    release_fingerprint,
    release_is_at_least,
    resolve_active_db_prefix,
)


def _release() -> dict[str, object]:
    return {
        "last_updated": "2026-08-19T00:00:00",
        "number_of_letters": 998_069_435_926,
        "number_of_sequences": 130_155_243,
        "number_of_volumes": 84,
        "bytes_total": 282_692_127_129,
    }


def test_release_fingerprint_is_provider_independent() -> None:
    ftp = {**_release(), "source_provider": "ncbi-direct"}
    s3 = {**_release(), "source_provider": "s3", "snapshot": "later-mirror"}
    assert release_fingerprint("core_nt", ftp) == release_fingerprint("core_nt", s3)


def test_generation_path_and_active_prefix_round_trip() -> None:
    fingerprint = release_fingerprint("core_nt", _release())
    generation = generation_id("ncbi-direct", "2026-08-19T00:00:00", fingerprint)
    prefix = generation_db_prefix("core_nt", generation)
    assert prefix.startswith("core_nt/generations/ncbi-direct-20260819-")
    assert resolve_active_db_prefix("core_nt", {"active_prefix": prefix}) == prefix


def test_active_prefix_defaults_to_legacy_and_rejects_escape() -> None:
    assert resolve_active_db_prefix("core_nt", {}) == "core_nt/core_nt"
    with pytest.raises(ValueError, match="invalid active_prefix"):
        resolve_active_db_prefix("core_nt", {"active_prefix": "../foreign/core_nt"})


def test_release_date_compares_direct_generation_with_s3_snapshot() -> None:
    assert release_is_at_least("ncbi-direct-20260819-0123456789ab", "2026-07-21-01-05-02")
    assert not release_is_at_least("2026-07-21-01-05-02", "2026-08-19T00:00:00")
