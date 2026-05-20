from __future__ import annotations

from api.services.blast_db_metadata import database_display_metadata_from_info, extract_db_name


def test_extract_db_name_handles_every_input_shape() -> None:
    assert extract_db_name("core_nt") == "core_nt"
    assert extract_db_name("blast-db/core_nt") == "core_nt"
    assert extract_db_name("blast-db/core_nt/core_nt") == "core_nt"
    assert (
        extract_db_name("https://elbstg01.blob.core.windows.net/blast-db/core_nt/core_nt")
        == "core_nt"
    )
    assert (
        extract_db_name("https://elbstg01.blob.core.windows.net/blast-db/core_nt/core_nt?ignored=1")
        == "core_nt"
    )
    assert extract_db_name("https://elbstg01.blob.core.windows.net/queries/q.fa") == ""
    assert extract_db_name("") == ""


def test_database_display_metadata_merges_core_nt_catalogue_with_storage_stats() -> None:
    metadata = database_display_metadata_from_info(
        "core_nt",
        {
            "source": "ncbi",
            "description": "Core nucleotide BLAST database",
            "source_version": "2026-05-18",
            "total_sequences": 125_929_380,
            "total_letters": 999_000_000,
        },
        fallback_database="https://elbstg01.blob.core.windows.net/blast-db/core_nt",
    )

    assert metadata["name"] == "core_nt"
    assert metadata["database"].endswith("/core_nt")
    assert metadata["title"] == "Core nucleotide BLAST database"
    assert metadata["description"].startswith("The core nucleotide BLAST database consists")
    assert metadata["molecule_type"] == "mixed DNA"
    assert metadata["update_date"] == "2026/05/18"
    assert metadata["number_of_sequences"] == 125_929_380
    assert metadata["number_of_letters"] == 999_000_000
    assert metadata["source_version"] == "2026-05-18"


def test_database_display_metadata_prefers_blastdb_metadata_title() -> None:
    metadata = database_display_metadata_from_info(
        "custom_db",
        {
            "title": "Lab isolates",
            "description": "Curated sequences",
            "molecule_type": "Nucleotide",
            "update_date": "2026/05/01",
            "number-of-sequences": "1,234",
        },
    )

    assert metadata["title"] == "Lab isolates"
    assert metadata["description"] == "Curated sequences"
    assert metadata["molecule_type"] == "mixed DNA"
    assert metadata["update_date"] == "2026/05/01"
    assert metadata["number_of_sequences"] == 1234
