"""Tests for the terminal sidecar's standalone `elb-cfg` helper.

Responsibility: Pin the INI generation / validation / blob-URL-expansion
contract of ``terminal/elb_cfg.py`` so it stays compatible with
``api/services/blast/config.py`` and upstream elastic-blast.
Edit boundaries: Loads the helper via importlib (the terminal image has no
``api`` package, and this module is not on the normal import path). Only test
``elb_cfg``'s public surface (``build_config``, ``expand_blob_reference``,
``missing_required``, ``main``).
Key entry points: pytest test functions below.
Risky contracts: Section/key names emitted here are the same contract the
Celery submit path relies on; renaming a section/key in the helper without
updating ``config.py`` is exactly what these tests guard against.
Validation: ``uv run pytest -q api/tests/test_elb_cfg_helper.py``.
"""

from __future__ import annotations

import configparser
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ELB_CFG_PATH = _REPO_ROOT / "terminal" / "elb_cfg.py"


def _load_elb_cfg() -> object:
    spec = importlib.util.spec_from_file_location("_elb_cfg_for_test", _ELB_CFG_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def elb_cfg(monkeypatch: pytest.MonkeyPatch) -> object:
    for var in (
        "AZURE_REGION",
        "AZURE_RESOURCE_GROUP",
        "STORAGE_ACCOUNT_NAME",
        "PLATFORM_ACR_NAME",
        "AZURE_STORAGE_BLOB_SUFFIX",
        "USER",
    ):
        monkeypatch.delenv(var, raising=False)
    return _load_elb_cfg()


def _build(elb_cfg: object, argv: list[str]) -> configparser.ConfigParser:
    args = elb_cfg.build_parser().parse_args(argv)
    elb_cfg._apply_env_defaults(args)
    return elb_cfg.build_config(args)


def test_generates_expected_sections(elb_cfg: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_REGION", "koreacentral")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-elb")
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stelb")
    cfg = _build(
        elb_cfg,
        [
            "--program",
            "blastn",
            "--db",
            "blast-db/16S/16S",
            "--queries",
            "q.fa",
            "--results",
            "run-1",
        ],
    )
    assert cfg.get("cloud-provider", "azure-region") == "koreacentral"
    assert cfg.get("cloud-provider", "azure-resource-group") == "rg-elb"
    assert cfg.get("cloud-provider", "azure-storage-account") == "stelb"
    assert cfg.get("cluster", "machine-type")
    assert cfg.get("cluster", "exp-use-local-ssd") == "true"
    assert cfg.get("blast", "program") == "blastn"
    assert cfg.get("blast", "db") == "blast-db/16S/16S"


def test_bare_query_expands_to_blob_url(elb_cfg: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stelb")
    cfg = _build(elb_cfg, ["--queries", "q.fa", "--results", "run-1"])
    assert cfg.get("blast", "queries") == "https://stelb.blob.core.windows.net/queries/q.fa"
    assert cfg.get("blast", "results") == "https://stelb.blob.core.windows.net/results/run-1"


def test_container_prefixed_path_not_double_wrapped(
    elb_cfg: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stelb")
    cfg = _build(elb_cfg, ["--queries", "queries/sub/q.fa"])
    assert cfg.get("blast", "queries") == "https://stelb.blob.core.windows.net/queries/sub/q.fa"


def test_full_url_passed_through(elb_cfg: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stelb")
    url = "https://other.blob.core.windows.net/queries/q.fa"
    cfg = _build(elb_cfg, ["--queries", url])
    assert cfg.get("blast", "queries") == url


def test_bare_value_kept_when_no_storage_account(elb_cfg: object) -> None:
    cfg = _build(elb_cfg, ["--queries", "q.fa"])
    # No storage account known -> do not invent a URL; surface the bare value.
    assert cfg.get("blast", "queries") == "q.fa"


def test_missing_required_flags_gaps(elb_cfg: object) -> None:
    cfg = _build(elb_cfg, [])
    gaps = elb_cfg.missing_required(cfg)
    # db / queries / results are empty here.
    joined = " ".join(gaps)
    assert "db" in joined
    assert "queries" in joined
    assert "results" in joined


def test_check_complete_file(
    elb_cfg: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZURE_REGION", "koreacentral")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-elb")
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stelb")
    cfg = _build(elb_cfg, ["--db", "blast-db/16S/16S", "--queries", "q.fa", "--results", "run-1"])
    target = tmp_path / "elastic-blast.ini"
    target.write_text(elb_cfg.config_to_text(cfg), encoding="utf-8")
    assert elb_cfg.check_config(str(target)) == 0


def test_check_incomplete_file(elb_cfg: object, tmp_path: Path) -> None:
    target = tmp_path / "bad.ini"
    target.write_text("[blast]\nprogram = blastn\n", encoding="utf-8")
    assert elb_cfg.check_config(str(target)) == 1


def test_main_writes_output_and_refuses_overwrite(
    elb_cfg: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORAGE_ACCOUNT_NAME", "stelb")
    out = tmp_path / "elastic-blast.ini"
    rc = elb_cfg.main(
        ["--db", "blast-db/16S/16S", "--queries", "q.fa", "--results", "run-1", "-o", str(out)]
    )
    assert rc == 0
    assert out.exists()
    # Second write without --force must refuse.
    rc2 = elb_cfg.main(["--queries", "q.fa", "-o", str(out)])
    assert rc2 == 1
    # With --force it overwrites.
    rc3 = elb_cfg.main(["--queries", "q.fa", "-o", str(out), "--force"])
    assert rc3 == 0


@pytest.mark.parametrize(
    "db",
    [
        "",
        "blast-db/16S/16S",
        "my-container/path/to/db",
        "https://acct.blob.core.windows.net/blast-db/16S/16S",
        "https://acct.blob.core.windows.net/custom/db",
    ],
)
def test_container_derivation_matches_backend(elb_cfg: object, db: str) -> None:
    """The helper must resolve the same azure-storage-account-container as the
    dashboard submit path (api.services.blast.config.generate_config)."""
    from api.services.blast.config import generate_config

    helper_container = elb_cfg._derive_storage_container(db)
    backend_ini = generate_config(
        {"db": db, "program": "blastn", "query_blob_url": "", "results_url": ""}
    )
    backend_cfg = configparser.ConfigParser()
    backend_cfg.read_string(backend_ini)
    backend_container = backend_cfg.get(
        "cloud-provider", "azure-storage-account-container", fallback=""
    )
    assert helper_container == backend_container


def test_container_set_even_without_storage_account(elb_cfg: object) -> None:
    cfg = _build(elb_cfg, ["--db", "blast-db/16S/16S"])
    assert cfg.get("cloud-provider", "azure-storage-account-container") == "blast-db"
