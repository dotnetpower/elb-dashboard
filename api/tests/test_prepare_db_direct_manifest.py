"""Tests for NCBI Direct prepare-db Kubernetes builders.

Responsibility: Lock Indexed Job bounds, generation-scoped destinations,
    archive-spec ConfigMaps, scratch sizing, and secure extraction guards.
Edit boundaries: Pure manifest/script assertions only; no Kubernetes calls.
Key entry points: Tests for `api.services.k8s.prepare_db_direct_jobs`.
Risky contracts: Parallelism stays capped, source specs remain immutable, and
    the script must verify MD5 before extracting or uploading.
Validation: `uv run pytest -q api/tests/test_prepare_db_direct_manifest.py`.
"""

import hashlib
import io
import subprocess
import sys
import tarfile
from pathlib import Path

from api.services.k8s.prepare_db_direct_jobs import (
    DIRECT_PREPARE_SCRIPT,
    build_direct_job_manifest,
    build_direct_scripts_configmap,
    direct_prepare_job_name,
)
from api.services.ncbi_direct import NcbiDirectArchive


def _archives() -> tuple[NcbiDirectArchive, ...]:
    return (
        NcbiDirectArchive(
            url="https://ftp.ncbi.nlm.nih.gov/blast/db/core_nt.00.tar.gz",
            md5_url="https://ftp.ncbi.nlm.nih.gov/blast/db/core_nt.00.tar.gz.md5",
            md5="0123456789abcdef0123456789abcdef",
            size=6 * 1024**3,
            member_prefix="core_nt",
        ),
        NcbiDirectArchive(
            url="https://ftp.ncbi.nlm.nih.gov/blast/db/core_nt.01.tar.gz",
            md5_url="https://ftp.ncbi.nlm.nih.gov/blast/db/core_nt.01.tar.gz.md5",
            md5="fedcba9876543210fedcba9876543210",
            size=5 * 1024**3,
            member_prefix="core_nt",
        ),
    )


def test_direct_configmap_pins_one_archive_per_index() -> None:
    manifest = build_direct_scripts_configmap(archives=_archives(), name="prepare-core-nt-direct")

    assert "prepare-direct.sh" in manifest["data"]
    assert "core_nt.00.tar.gz" in manifest["data"]["archive-00.json"]
    assert "fedcba9876543210fedcba9876543210" in manifest["data"]["archive-01.json"]


def test_direct_job_caps_parallelism_and_sizes_scratch() -> None:
    manifest = build_direct_job_manifest(
        job_name="prepare-core-nt-direct",
        db_name="core_nt",
        storage_account="stelbtest01",
        generation_id="ncbi-direct-20260819-0123456789ab",
        destination_prefix="core_nt/generations/ncbi-direct-20260819-0123456789ab",
        transfer_manifest_sha256="a" * 64,
        archive_count=2,
        scripts_configmap="prepare-core-nt-direct",
        parallelism=2,
        max_archive_size=6 * 1024**3,
    )

    spec = manifest["spec"]
    assert spec["completions"] == 2
    assert spec["parallelism"] == 2
    assert spec["backoffLimitPerIndex"] == 2
    pod = spec["template"]["spec"]
    assert pod["nodeSelector"] == {"kubernetes.azure.com/mode": "user"}
    container = pod["containers"][0]
    assert container["resources"]["requests"]["ephemeral-storage"] == "36Gi"
    assert pod["topologySpreadConstraints"][0]["topologyKey"] == "kubernetes.io/hostname"


def test_direct_script_verifies_before_upload_and_rejects_links() -> None:
    assert DIRECT_PREPARE_SCRIPT.index("archive MD5 mismatch") < DIRECT_PREPARE_SCRIPT.index(
        "azcopy login"
    )
    assert "not member.isfile()" in DIRECT_PREPARE_SCRIPT
    assert "name != os.path.basename(name)" in DIRECT_PREPARE_SCRIPT
    assert "archive expansion exceeded the bounded ratio" in DIRECT_PREPARE_SCRIPT
    assert ".manifests/${INDEX}.json" in DIRECT_PREPARE_SCRIPT


def _extraction_python() -> str:
    start = DIRECT_PREPARE_SCRIPT.index("import hashlib\n")
    end = DIRECT_PREPARE_SCRIPT.index("\nPY\nrm -f", start)
    return DIRECT_PREPARE_SCRIPT[start:end]


def _write_archive(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as bundle:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))


def _extract_archive(tmp_path: Path, files: dict[str, bytes], member_prefix: str) -> set[str]:
    archive = tmp_path / "archive.tar.gz"
    output = tmp_path / "out"
    output.mkdir()
    _write_archive(archive, files)
    payload = archive.read_bytes()
    subprocess.run(  # noqa: S603 - fixed interpreter executes the shipped script fixture
        [
            sys.executable,
            "-c",
            _extraction_python(),
            str(archive),
            str(output),
            member_prefix,
            hashlib.md5(payload, usedforsecurity=False).hexdigest(),
            str(len(payload)),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return {path.name for path in output.iterdir()}


def test_direct_extraction_skips_taxonomy_embedded_in_search_database(tmp_path: Path) -> None:
    extracted = _extract_archive(
        tmp_path,
        {
            "16S_ribosomal_RNA.nhr": b"database",
            "taxdb.btd": b"embedded taxonomy",
            "taxdb.bti": b"embedded taxonomy",
            "taxonomy4blast.sqlite3": b"embedded taxonomy",
        },
        "16S_ribosomal_RNA",
    )

    assert extracted == {"16S_ribosomal_RNA.nhr", ".files.json"}


def test_direct_extraction_accepts_exact_standalone_taxonomy_members(tmp_path: Path) -> None:
    extracted = _extract_archive(
        tmp_path,
        {
            "taxdb.btd": b"taxonomy",
            "taxdb.bti": b"taxonomy",
            "taxonomy4blast.sqlite3": b"taxonomy",
        },
        "taxdb",
    )

    assert extracted == {
        "taxdb.btd",
        "taxdb.bti",
        "taxonomy4blast.sqlite3",
        ".files.json",
    }


def test_direct_extraction_rejects_unrelated_archive_member(tmp_path: Path) -> None:
    archive = tmp_path / "archive.tar.gz"
    output = tmp_path / "out"
    output.mkdir()
    _write_archive(archive, {"16S_ribosomal_RNA.nhr": b"db", "unexpected.txt": b"bad"})
    payload = archive.read_bytes()

    result = subprocess.run(  # noqa: S603 - fixed interpreter executes the shipped script fixture
        [
            sys.executable,
            "-c",
            _extraction_python(),
            str(archive),
            str(output),
            "16S_ribosomal_RNA",
            hashlib.md5(payload, usedforsecurity=False).hexdigest(),
            str(len(payload)),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsafe archive member" in result.stderr


def test_direct_job_name_is_deterministic_and_kubernetes_safe() -> None:
    name = direct_prepare_job_name("a" * 64, "ncbi-direct-20260819-0123456789ab")
    assert len(name) <= 63
    assert name == direct_prepare_job_name("a" * 64, "ncbi-direct-20260819-0123456789ab")


def test_direct_configmap_stays_below_safety_cap_for_core_nt_scale() -> None:
    archive = _archives()[0]
    manifest = build_direct_scripts_configmap(
        archives=tuple(archive for _ in range(84)),
        name="prepare-core-nt-direct",
    )
    assert sum(len(value.encode("utf-8")) for value in manifest["data"].values()) < 900 * 1024
