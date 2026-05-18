from __future__ import annotations

from api.services.blast_db_metadata import extract_db_name


def test_extract_db_name_handles_every_input_shape() -> None:
    assert extract_db_name("core_nt") == "core_nt"
    assert extract_db_name("blast-db/core_nt") == "core_nt"
    assert extract_db_name("blast-db/core_nt/core_nt") == "core_nt"
    assert (
        extract_db_name("https://elbstg01.blob.core.windows.net/blast-db/core_nt/core_nt")
        == "core_nt"
    )
    assert (
        extract_db_name(
            "https://elbstg01.blob.core.windows.net/blast-db/core_nt/core_nt?ignored=1"
        )
        == "core_nt"
    )
    assert extract_db_name("https://elbstg01.blob.core.windows.net/queries/q.fa") == ""
    assert extract_db_name("") == ""
