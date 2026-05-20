"""Tests for Terminal Patch Elastic BLAST behavior.

Responsibility: Tests for Terminal Patch Elastic BLAST behavior
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `_load_patch_module`,
`test_patch_init_shard_script_writes_hardened_cache_skip`,
`test_patch_init_shard_script_is_idempotent`,
`test_patch_init_shard_script_updates_installed_package_copy`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_terminal_patch_elastic_blast.py`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_patch_module():
    module_path = Path(__file__).resolve().parents[2] / "terminal" / "patch_elastic_blast.py"
    spec = importlib.util.spec_from_file_location("terminal_patch_elastic_blast", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_patch_init_shard_script_writes_hardened_cache_skip(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    script_path = tmp_path / "src" / "elastic_blast" / "templates" / "scripts"
    script_path.mkdir(parents=True)
    target = script_path / "init-db-shard-aks.sh"
    target.write_text("#!/bin/bash\ntouch .download-complete\n")

    patch_module.patch_init_shard_script(tmp_path)

    text = target.read_text()
    skip_prefix = text.split('echo "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}"', 1)[0]
    assert 'cd "${ELB_BLASTDB_DIR:-/blast/blastdb}"' in text
    assert "CLEANUP partial downloads" in text
    assert "find . -maxdepth 1 -name '.azDownload-*' -exec rm -rf {} +" in text
    assert "[ -f .download-complete ]" in text
    assert "[ ! -s taxdb.btd ]" in text
    assert "[ ! -s taxdb.bti ]" in text
    assert "CACHE_INCOMPLETE missing ${volume}.${payload_ext}" in text
    assert "CACHE_STALE source-version mismatch" in text
    assert "Resolving DB source version: ${METADATA_URL}" in text
    assert "${DB_BASE_URL}${ORIG_DB}-metadata.json" in text
    assert "WARNING: DB metadata source-version lookup failed" in text
    assert "write_volpaths" in text
    assert "printf '%s' ok > .download-complete" in text
    assert "printf '%s' \"$EXPECTED_SOURCE_VERSION\" > .download-source-version" in text
    assert "if [ -s .download-complete ]" not in text
    assert "touch .download-complete" not in text
    assert "taxonomy4blast.sqlite3" not in skip_prefix


def test_patch_init_shard_script_is_idempotent(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    script_path = tmp_path / "src" / "elastic_blast" / "templates" / "scripts"
    script_path.mkdir(parents=True)
    target = script_path / "init-db-shard-aks.sh"
    target.write_text("legacy\n")

    patch_module.patch_init_shard_script(tmp_path)
    once = target.read_text()
    patch_module.patch_init_shard_script(tmp_path)

    assert target.read_text() == once


def test_patch_init_shard_script_updates_installed_package_copy(tmp_path: Path) -> None:
    patch_module = _load_patch_module()
    source_dir = tmp_path / "src" / "elastic_blast" / "templates" / "scripts"
    installed_dir = (
        tmp_path
        / "venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "elastic_blast"
        / "templates"
        / "scripts"
    )
    source_dir.mkdir(parents=True)
    installed_dir.mkdir(parents=True)
    source_target = source_dir / "init-db-shard-aks.sh"
    installed_target = installed_dir / "init-db-shard-aks.sh"
    source_target.write_text("source legacy\n")
    installed_target.write_text("installed legacy\n")

    patch_module.patch_init_shard_script(tmp_path)

    for target in (source_target, installed_target):
        text = target.read_text()
        assert "Resolving DB source version: ${METADATA_URL}" in text
        assert "DOWNLOAD_SKIP existing shard=${ELB_SHARD_IDX}" in text
        assert "source legacy" not in text
        assert "installed legacy" not in text
