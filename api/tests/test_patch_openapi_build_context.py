"""Tests for the OpenAPI image build-context patcher.

Responsibility: Verify OpenAPI image patching enforces runtime policy and refreshes stale
ElasticBLAST scripts.
Edit boundaries: Use temporary build contexts only; never invoke Docker or Azure.
Key entry points: `test_patch_dockerfile_asserts_ttl_in_all_runtime_copies`,
`test_patch_removes_obsolete_precise_search_space_guard`,
`test_patch_app_reconciles_elb_scripts_by_content`,
`test_patch_allows_only_canonical_merged_result_through_blob_path_guard`,
`test_patch_submit_runtime_id_rejects_noncanonical_correlation`.
Risky contracts: The assertions must cover source, system Python, and venv templates; OpenAPI
submits must never trust historical warmup Jobs or name-only ConfigMap checks as node-local
cache-presence proof. Missing precise search space must reach the active-generation fallback. Result
path guards must continue rejecting traversal and arbitrary files.
Validation: `uv run pytest -q api/tests/test_patch_openapi_build_context.py`.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts/dev/patch-openapi-build-context.py"
    spec = importlib.util.spec_from_file_location("patch_openapi_build_context", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_configmap_reconciliation_fixture(
    tmp_path: Path,
) -> tuple[
    Any,
    dict[str, Any],
    list[tuple[object, dict[str, Any]]],
    list[tuple[object, ...]],
    dict[str, str],
]:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def _ensure_elb_scripts_configmap() -> None:\n"
        "    required_scripts = {'blast-run-aks.sh'}\n"
        "    data = {'blast-run-aks.sh': 'stale'}\n"
        "    if required_scripts.issubset(set(data)):\n"
        "        return\n"
        "\n\n"
        "def _run_submit_bg(job_id: str) -> None:\n"
        "    pass\n"
    )
    module._harden_elb_scripts_configmap_reconciliation(path)
    patched = path.read_text()
    module._harden_elb_scripts_configmap_reconciliation(path)
    assert path.read_text() == patched

    scripts_path = tmp_path / "templates" / "scripts"
    scripts_path.mkdir(parents=True)
    required_scripts = {
        "blast-run-aks.sh",
        "elb-finalizer-aks.sh",
        "init-db-download-aks.sh",
        "init-db-shard-aks.sh",
        "query-download-ssd-aks.sh",
        "results-export-aks.sh",
    }
    desired_data = {}
    for name in required_scripts:
        content = f"#!/bin/bash\necho secret-{name}\n"
        (scripts_path / name).write_text(content)
        desired_data[name] = content

    state: dict[str, Any] = {
        "existing_data": dict(desired_data),
        "lookup_error": None,
        "run_error_at": None,
        "post_apply_data": None,
    }
    subprocess_calls: list[tuple[object, dict[str, Any]]] = []
    log_messages: list[tuple[object, ...]] = []

    def safe_exec(_command: object, **_kwargs: Any) -> SimpleNamespace:
        if state["lookup_error"] is not None:
            lookup_error = state["lookup_error"]
            state["lookup_error"] = None
            raise lookup_error
        return SimpleNamespace(stdout=json.dumps({"data": state["existing_data"]}))

    def run(command: object, **kwargs: Any) -> SimpleNamespace:
        subprocess_calls.append((command, kwargs))
        if state["run_error_at"] == len(subprocess_calls):
            raise RuntimeError("kubectl apply failed")
        if len(subprocess_calls) == 2:
            post_apply_data = state["post_apply_data"]
            state["existing_data"] = dict(
                desired_data if post_apply_data is None else post_apply_data
            )
        return SimpleNamespace(stdout="apiVersion: v1\nkind: ConfigMap\n")

    namespace: dict[str, Any] = {
        "Path": Path,
        "files": lambda _package: tmp_path,
        "json": json,
        "logger": SimpleNamespace(info=lambda *args: log_messages.append(args)),
        "safe_exec": safe_exec,
        "subprocess": SimpleNamespace(run=run),
    }
    function_source = patched[
        patched.index("def _ensure_elb_scripts_configmap()") : patched.index(
            "\n\ndef _run_submit_bg"
        )
    ]
    exec(function_source, namespace)  # noqa: S102 - generated temporary fixture code.
    return (
        namespace["_ensure_elb_scripts_configmap"],
        state,
        subprocess_calls,
        log_messages,
        desired_data,
    )


def test_patch_dockerfile_asserts_ttl_in_all_runtime_copies(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("stub\n")
    (tmp_path / "Dockerfile").write_text(
        "ARG ELB_REF=old\n"
        "COPY ./app /app\n"
        "RUN true && \\\n"
        "    git -C /tmp/elb-src checkout ${ELB_REF} && \\\n"
        "    rm -rf /tmp/elb-src && \\\n"
        "    pip3 install --no-cache-dir --no-build-isolation /tmp/elb-src && \\\n"
        "    true\n"
        "RUN true \\\n"
        "    && pip install --no-cache-dir azure-cli \\\n"
        "    && true\n"
    )

    module.patch_dockerfile(tmp_path)

    text = (tmp_path / "Dockerfile").read_text()
    module.patch_dockerfile(tmp_path)
    assert (tmp_path / "Dockerfile").read_text() == text
    assert "ARG ELB_REF=744d79b" in text
    assert text.count("grep -q 'ttlSecondsAfterFinished:'") == 3
    assert text.count("for template in job-init-ssd-shard-aks.yaml.template") == 3
    assert text.count('elb-job-id: "${BLAST_ELB_JOB_ID}"') == 3
    assert text.count("grep -q 'def _wait_for_elb_init_jobs('") == 3
    assert text.count("grep -q 'ELB DB reader lock'") == 3
    assert text.count("grep -q 'name: ELB_DB_READER_LOCK'") == 3
    assert text.count("grep -q 'DISK_PREFLIGHT required_bytes='") == 3
    assert text.count("grep -q 'ELB DB writer lock'") == 3
    assert text.count("grep -q 'name: ELB_DB_WRITER_LOCK'") == 3
    assert text.count("for template in blast-batch-job-local-ssd-aks.yaml.template") == 3
    assert text.count("grep -Fq 'name: init-ssd-${BLAST_ELB_JOB_ID}-${NODE_ORDINAL}'") == 3
    source_templates = "/tmp/elb-src/src/elastic_blast/templates/"  # noqa: S108
    assert source_templates in text
    assert "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/" in text
    assert "/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/" in text


def test_patch_dockerfile_upgrades_legacy_patched_context(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("stub\n")
    (tmp_path / "Dockerfile").write_text(
        "ARG ELB_REF=7a471297\n"
        "COPY ./app /app\n"
        "COPY patch_elastic_blast.py /tmp/patch_elastic_blast.py\n"
        "COPY merge-sharded-results.sh /tmp/merge-sharded-results.sh\n"
        "RUN true && \\\n"
        "    git -C /tmp/elb-src checkout ${ELB_REF} && \\\n"
        "    python3 /tmp/patch_elastic_blast.py /tmp/elb-src /tmp/merge-sharded-results.sh && \\\n"
        "    true && \\\n"
        "    pip3 install --no-cache-dir --no-build-isolation /tmp/elb-src && \\\n"
        "    cp -a /tmp/elb-src/src/elastic_blast/templates/. "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/ && \\\n"
        "    true\n"
        "RUN true \\\n"
        "    && pip install --no-cache-dir azure-cli \\\n"
        "    && pip install --no-cache-dir --no-deps --no-build-isolation /tmp/elb-src \\\n"
        "    && cp -a /tmp/elb-src/src/elastic_blast/templates/. "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/ \\\n"
        "    && rm -rf /tmp/elb-src \\\n"
        "    && true\n"
    )

    module.patch_dockerfile(tmp_path)

    text = (tmp_path / "Dockerfile").read_text()
    assert "ARG ELB_REF=744d79b" in text
    assert text.count("python3 /tmp/patch_elastic_blast.py") == 1
    assert text.count("cp -a /tmp/elb-src/src/elastic_blast/templates/.") == 2
    assert text.count("grep -q 'ttlSecondsAfterFinished:'") == 3
    assert text.count("for template in job-init-ssd-shard-aks.yaml.template") == 3
    assert text.count('elb-job-id: "${BLAST_ELB_JOB_ID}"') == 3
    assert text.count("grep -q 'def _wait_for_elb_init_jobs('") == 3
    assert text.count("grep -q 'ELB DB reader lock'") == 3
    assert text.count("grep -q 'name: ELB_DB_READER_LOCK'") == 3
    assert text.count("grep -q 'DISK_PREFLIGHT required_bytes='") == 3
    assert text.count("grep -q 'ELB DB writer lock'") == 3
    assert text.count("grep -q 'name: ELB_DB_WRITER_LOCK'") == 3
    assert text.count("for template in blast-batch-job-local-ssd-aks.yaml.template") == 3
    assert text.count("grep -Fq 'name: init-ssd-${BLAST_ELB_JOB_ID}-${NODE_ORDINAL}'") == 3


def test_patch_app_adds_runtime_id_to_terminal_webhook(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def notify(job_id, job_snap, updates, payload):\n"
        "    if job_snap:\n"
        "            merged = {**job_snap, **updates}\n"
        "            started_at = merged.get('started_at')\n"
    )

    module._patch_terminal_webhook_runtime_id(path)
    module._harden_openapi_runtime_id_consumers(path)
    first = path.read_text()
    module._patch_terminal_webhook_runtime_id(path)
    module._harden_openapi_runtime_id_consumers(path)

    assert path.read_text() == first
    assert first.count('payload["elb_job_id"] = runtime_job_id') == 1
    assert 're.fullmatch(r"job-[0-9a-f]{32}", runtime_job_id, re.IGNORECASE)' in first


def test_patch_app_disables_warmed_cache_skip(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        '    config["cluster"]["exp-skip-warmed-ssd-init"] = "true"\n'
        '    config["blast"]["db"] = db_url\n'
    )

    module._disable_warmed_cache_skip(path)
    first = path.read_text()
    module._disable_warmed_cache_skip(path)

    assert path.read_text() == first
    assert "Completed warmup Jobs are not node-local cache-presence proofs." in first
    assert first.rfind('config["cluster"]["exp-skip-warmed-ssd-init"] = "true"') < (
        first.rfind('config["cluster"].pop("exp-skip-warmed-ssd-init", None)')
    )


def test_copy_app_overlay_includes_exact_oracle(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / "app").mkdir()

    module._copy_app_overlay(tmp_path)

    assert (tmp_path / "app" / "eta.py").is_file()
    exact = tmp_path / "app" / "exact_oracle.py"
    assert exact.is_file()
    assert "def attach_db_order_oracle(" in exact.read_text()


def test_patch_source_wires_exact_oracle_before_dispatch() -> None:
    module = _load_module()
    source = Path(module.__file__).read_text()

    assert "import exact_oracle as _exact_oracle" in source
    assert "active_database = _exact_oracle.read_active_database(" in source
    assert "active_database.db_prefix" in source
    assert "active_database.shard_layout_prefix" in source
    assert "prepare_web_blast_statistics(" in source
    assert "select_web_blast_partitions(" in source
    assert "read_one_shard_layout(" in source
    assert "validate_web_blast_execution_options(" in source
    assert "opts, program=req.program" in source
    assert 'config["blast"]["disk-backed-monolithic"] = "true"' in source
    assert 'config["blast"]["mem-request"] = "104Gi"' in source
    assert 'config["blast"]["mem-limit"] = "112Gi"' in source
    assert "attach_web_blast_statistics(" in source
    assert "exact_oracle_info = _exact_oracle.attach_db_order_oracle(" in source
    assert "expected_source_version=active_database.source_version" in source
    assert '"source": "active_generation"' in source
    assert 'job_data["exact_oracle"] = exact_oracle_info' in source
    assert 'job_data["web_blast_statistics"] = web_blast_statistics.as_dict()' in source
    assert 'for _runtime_key in ("exact_oracle", "web_blast_statistics")' in source
    assert '"candidate_selection": (' in source
    assert '"monolithic_full_database"' in source
    assert '"db_partitions": partitions' in source
    assert '"disk_backed_bounded"' in source
    assert '"memory_request": "104Gi"' in source
    assert '"memory_limit": "112Gi"' in source
    assert "exact_oracle_info.update(one_shard_layout.as_dict())" in source
    assert source.index("prepare_web_blast_statistics(") < source.index(
        "select_web_blast_partitions("
    )


def test_patch_external_submit_preserves_parity_options(tmp_path: Path) -> None:
    module = _load_module()
    app = tmp_path / "app"
    app.mkdir()
    schemas = app / "schemas.py"
    schemas.write_text(
        "from pydantic import BaseModel, Field\n\n"
        "class ExternalBlastOptions(BaseModel):\n"
        "    dust: bool = Field(True)\n"
    )
    main = app / "main.py"
    main.write_text(
        "def _build_external_options(opts):\n"
        "    parts = [\n"
        '        "-dust yes" if opts.dust else "-dust no",\n'
        "    ]\n"
        "    return parts\n\n"
        "def external_submit(req):\n"
        "    result = dict(\n"
        '        extra=f"-word_size {req.options.word_size} '
        "{'-dust yes' if req.options.dust else '-dust no'}\",\n"
        "    )\n"
        "    internal = JobSubmitRequest(\n"
        "        program=req.program,\n"
        "    )\n"
        "    return result\n"
    )

    module._patch_external_soft_masking(tmp_path)
    first_schema = schemas.read_text()
    first_main = main.read_text()
    module._patch_external_soft_masking(tmp_path)

    assert schemas.read_text() == first_schema
    assert main.read_text() == first_main
    assert "soft_masking: bool = Field(False)" in first_schema
    assert "db_effective_search_space: Optional[int] = Field(None, ge=1)" in first_schema
    assert "class WebBlastStatisticalContext(BaseModel):" in first_schema
    assert '"-soft_masking false"' in first_main
    assert "req.options.soft_masking" in first_main
    assert 'parts.append(f"-searchsp {opts.db_effective_search_space}")' in first_main
    assert 'f" -searchsp {req.options.db_effective_search_space}"' in first_main
    assert "opts.web_blast_statistical_context.filtered_database_letters" in first_main
    assert "web_blast_statistical_context=(" in first_main
    ast.parse(first_schema)
    ast.parse(first_main)


def test_patch_replaces_stale_core_nt_search_space_fallback(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        '        if "-searchsp" not in opts and "-dbsize" not in opts:\n'
        '            config["blast"]["options"] = f"{opts} -searchsp 32156241807668"\n'
    )

    module._replace_stale_core_nt_search_space_fallback(path)
    first = path.read_text()
    module._replace_stale_core_nt_search_space_fallback(path)

    assert path.read_text() == first
    assert "32156241807668" not in first
    assert "Precise core_nt sharding requires db_effective_search_space" not in first
    assert "read_active_database(" in first
    assert "preserve_or_set_search_space(" in first
    assert first.index("read_active_database(") < first.index("preserve_or_set_search_space(")
    assert "prepare_web_blast_statistics(" in first
    assert "select_web_blast_partitions(" in first
    assert "read_one_shard_layout(" in first
    assert 'config["blast"]["db-partitions"] = str(partitions)' in first
    assert 'config["blast"]["disk-backed-monolithic"] = "true"' in first
    assert 'config["blast"]["mem-request"] = "104Gi"' in first
    assert 'config["blast"]["mem-limit"] = "112Gi"' in first
    assert "opts, active_database.search_space" in first
    assert "active_database.db_prefix" in first
    assert "active_database.shard_layout_prefix" in first
    assert "Active database statistics are required for precise core_nt sharding" in first


def test_patch_removes_obsolete_precise_search_space_guard(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        '        if "-searchsp" not in opts and "-dbsize" not in opts:\n'
        '            config["blast"]["options"] = f"{opts} -searchsp 32156241807668"\n'
    )
    module._replace_stale_core_nt_search_space_fallback(path)
    active_fallback = path.read_text()
    obsolete_guard = (
        '        if "-searchsp" not in opts and "-dbsize" not in opts:\n'
        "            raise HTTPException(\n"
        "                400,\n"
        '                "Precise core_nt sharding requires db_effective_search_space",\n'
        "            )\n"
    )
    path.write_text(active_fallback.replace("        try:\n", obsolete_guard + "        try:\n", 1))

    module._replace_stale_core_nt_search_space_fallback(path)

    assert path.read_text() == active_fallback


def test_patch_adds_candidate_selection_evidence_idempotently(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "            exact_oracle_info = _exact_oracle.attach_db_order_oracle(\n"
        "                token=token,\n"
        "            ).as_dict()\n"
        "            if web_blast_statistics is not None:\n"
        "                upload_statistics()\n"
    )

    module._patch_web_blast_candidate_selection_evidence(path)
    first = path.read_text()
    module._patch_web_blast_candidate_selection_evidence(path)

    assert path.read_text() == first
    assert first.count('"candidate_selection": (') == 1
    assert '"monolithic_full_database"' in first
    assert '"db_partitions": partitions' in first
    assert '"disk_backed_bounded"' in first
    assert '"memory_request": "104Gi"' in first
    assert '"memory_limit": "112Gi"' in first
    assert "exact_oracle_info.update(one_shard_layout.as_dict())" in first
    assert "validate_web_blast_execution_options(" in first
    assert "opts, program=req.program" in first


def test_patch_upgrades_legacy_candidate_selection_evidence(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "            exact_oracle_info = _exact_oracle.attach_db_order_oracle(\n"
        "                token=token,\n"
        "            ).as_dict()\n"
        "            exact_oracle_info.update(\n"
        "                {\n"
        '                    "candidate_selection": (\n'
        '                        "monolithic_full_database"\n'
        "                        if partitions == 1\n"
        '                        else "partitioned_shards"\n'
        "                    ),\n"
        '                    "db_partitions": partitions,\n'
        "                }\n"
        "            )\n"
        "            if web_blast_statistics is not None:\n"
        "                upload_statistics()\n"
    )

    module._patch_web_blast_candidate_selection_evidence(path)

    text = path.read_text()
    assert text.count('"candidate_selection": (') == 1
    assert '"memory_mode": (' in text
    assert '"memory_request": "104Gi"' in text
    assert '"memory_limit": "112Gi"' in text
    assert "validate_web_blast_execution_options(" in text
    assert "opts, program=req.program" in text


def test_patch_prefers_canonical_merged_result_and_rechecks_shard_cache(
    tmp_path: Path,
) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def _list_result_files(job_info):\n"
        '    existing = job_info.get("result_files")\n'
        "    if isinstance(existing, list) and existing:\n"
        "        return existing\n"
        "    files = []\n"
        "    seen = set()\n"
        '    for name in ["batch_1.out.gz"]:\n'
        '        if not name.startswith("batch_"):\n'
        "            continue\n"
    )

    module._patch_canonical_merged_result_discovery(path)
    first = path.read_text()
    module._patch_canonical_merged_result_discovery(path)

    assert path.read_text() == first
    assert 'item.get("filename") == "merged_results.out.gz"' in first
    assert 'if name == "merged_results.out.gz":' in first
    assert "files = []" in first
    assert "seen = set()" in first


def test_patch_allows_only_canonical_merged_result_through_blob_path_guard(
    tmp_path: Path,
) -> None:
    module = _load_module()
    path = tmp_path / "helpers.py"
    path.write_text(
        "def _safe_result_blob_path(value: str, fallback_filename: str) -> str:\n"
        '    blob_path = str(value or fallback_filename).strip().lstrip("/")\n'
        '    if ".." in blob_path or "?" in blob_path or "#" in blob_path:\n'
        '        raise HTTPException(400, "Invalid result blob path")\n'
        "    if not re.match("
        'r"^[A-Za-z0-9._/-]{1,512}\\.(?:xml|out)(?:\\.gz)?$", '
        "blob_path, re.IGNORECASE):\n"
        '        raise HTTPException(400, "Invalid result blob path")\n'
        '    if not blob_path.split("/")[-1].startswith("batch_"):\n'
        '        raise HTTPException(400, "Invalid result blob path")\n'
        "    return blob_path\n"
    )

    module._patch_canonical_merged_result_validation(path)
    first = path.read_text()
    module._patch_canonical_merged_result_validation(path)

    assert path.read_text() == first
    ast.parse(first)

    class FakeHTTPException(Exception):
        pass

    namespace: dict[str, Any] = {"HTTPException": FakeHTTPException, "re": re}
    exec(first, namespace)  # noqa: S102 - generated temporary fixture code.
    validate = namespace["_safe_result_blob_path"]

    assert validate("job-runtime/merged_results.out.gz", "ignored.out.gz") == (
        "job-runtime/merged_results.out.gz"
    )
    assert validate("job-runtime/batch_001.out.gz", "ignored.out.gz") == (
        "job-runtime/batch_001.out.gz"
    )
    with pytest.raises(FakeHTTPException):
        validate("job-runtime/other.out.gz", "ignored.out.gz")
    with pytest.raises(FakeHTTPException):
        validate("../merged_results.out.gz", "ignored.out.gz")


def test_patch_app_rejects_late_warmed_cache_skip_assignment(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        '    config["blast"]["db"] = db_url\n'
        '    config["cluster"]["exp-skip-warmed-ssd-init"] = "true"\n'
    )

    with pytest.raises(RuntimeError, match="assignment appears after"):
        module._disable_warmed_cache_skip(path)


def test_patch_app_rejects_marker_without_warmed_cache_removal(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        '    config["blast"]["db"] = db_url\n'
        "    # Completed warmup Jobs are not node-local cache-presence proofs.\n"
    )

    with pytest.raises(RuntimeError, match="safety removal is missing"):
        module._disable_warmed_cache_skip(path)


def test_patch_app_replaces_name_only_configmap_check_idempotently(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def _ensure_elb_scripts_configmap() -> None:\n"
        "    required_scripts = {'blast-run-aks.sh'}\n"
        "    data = {'blast-run-aks.sh': 'stale'}\n"
        "    if required_scripts.issubset(set(data)):\n"
        "        return\n"
        "\n\n"
        "def _run_submit_bg(job_id: str) -> None:\n"
        "    pass\n"
    )

    module._harden_elb_scripts_configmap_reconciliation(path)
    first = path.read_text()
    module._harden_elb_scripts_configmap_reconciliation(path)

    assert path.read_text() == first
    assert "required_scripts.issubset(set(data))" not in first
    assert '"init-db-shard-aks.sh",' in first


def test_configmap_exact_content_skips_apply(tmp_path: Path) -> None:
    reconcile, _state, subprocess_calls, log_messages, _desired = (
        _build_configmap_reconciliation_fixture(tmp_path)
    )

    reconcile()

    assert subprocess_calls == []
    assert log_messages == []


def test_configmap_missing_entry_triggers_apply(tmp_path: Path) -> None:
    reconcile, state, subprocess_calls, log_messages, desired = (
        _build_configmap_reconciliation_fixture(tmp_path)
    )
    state["existing_data"].pop("init-db-shard-aks.sh")

    reconcile()

    assert len(subprocess_calls) == 2
    assert subprocess_calls[1][1]["input"] == "apiVersion: v1\nkind: ConfigMap\n"
    rendered_logs = repr(log_messages)
    assert "init-db-shard-aks.sh" in rendered_logs
    assert all(content not in rendered_logs for content in desired.values())


def test_configmap_stale_content_triggers_apply(tmp_path: Path) -> None:
    reconcile, state, subprocess_calls, log_messages, desired = (
        _build_configmap_reconciliation_fixture(tmp_path)
    )
    state["existing_data"]["init-db-shard-aks.sh"] = "#!/bin/bash\necho stale-secret\n"

    reconcile()

    assert len(subprocess_calls) == 2
    rendered_logs = repr(log_messages)
    assert "init-db-shard-aks.sh" in rendered_logs
    assert "stale-secret" not in rendered_logs
    assert all(content not in rendered_logs for content in desired.values())


def test_configmap_lookup_failure_reconciles_without_logging_detail(tmp_path: Path) -> None:
    reconcile, state, subprocess_calls, log_messages, desired = (
        _build_configmap_reconciliation_fixture(tmp_path)
    )
    state["lookup_error"] = RuntimeError("Bearer secret-value was rejected")

    reconcile()

    assert len(subprocess_calls) == 2
    rendered_logs = repr(log_messages)
    assert "RuntimeError" in rendered_logs
    assert "secret-value" not in rendered_logs
    assert all(content not in rendered_logs for content in desired.values())


def test_configmap_apply_failure_propagates(tmp_path: Path) -> None:
    reconcile, state, subprocess_calls, _log_messages, _desired = (
        _build_configmap_reconciliation_fixture(tmp_path)
    )
    state["existing_data"].pop("init-db-shard-aks.sh")
    state["run_error_at"] = 2

    with pytest.raises(RuntimeError, match="kubectl apply failed"):
        reconcile()

    assert len(subprocess_calls) == 2


def test_configmap_post_apply_drift_fails_closed(tmp_path: Path) -> None:
    reconcile, state, subprocess_calls, _log_messages, desired = (
        _build_configmap_reconciliation_fixture(tmp_path)
    )
    state["existing_data"].pop("init-db-shard-aks.sh")
    state["post_apply_data"] = {
        **desired,
        "init-db-shard-aks.sh": "#!/bin/bash\necho concurrent-stale\n",
    }

    with pytest.raises(
        RuntimeError,
        match=r"verification found drift scripts=init-db-shard-aks\.sh",
    ):
        reconcile()

    assert len(subprocess_calls) == 2


def test_configmap_oversized_scripts_fail_before_kubectl(tmp_path: Path) -> None:
    reconcile, _state, subprocess_calls, _log_messages, _desired = (
        _build_configmap_reconciliation_fixture(tmp_path)
    )
    (tmp_path / "templates/scripts/init-db-shard-aks.sh").write_text("x" * 900_001)

    with pytest.raises(RuntimeError, match="exceed ConfigMap limit"):
        reconcile()

    assert subprocess_calls == []


def test_missing_installed_script_fails_before_configmap_lookup(tmp_path: Path) -> None:
    reconcile, _state, subprocess_calls, _log_messages, _desired = (
        _build_configmap_reconciliation_fixture(tmp_path)
    )
    (tmp_path / "templates/scripts/init-db-shard-aks.sh").unlink()

    with pytest.raises(RuntimeError, match="Installed ElasticBLAST scripts are incomplete"):
        reconcile()

    assert subprocess_calls == []


def test_patch_app_restricts_runtime_ids_to_canonical_values(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def _discover_elb_job_id_from_submit_output(job_id: str, stdout: str) -> str:\n"
        "    if not stdout:\n"
        '        return ""\n'
        "    patterns = (\n"
        '        rf"/results/{re.escape(job_id)}/(?P<elb_job_id>job-[A-Za-z0-9_-]+)/metadata/",\n'
        '        r"\\b(?P<elb_job_id>job-[0-9a-f]{32})\\b",\n'
        "    )\n"
        "    for pattern in patterns:\n"
        "        match = re.search(pattern, stdout)\n"
        "        if match:\n"
        '            return match.group("elb_job_id")\n'
        '    return ""\n'
        "\n\n"
        "def _effective_elb_job_id(job_info: dict[str, Any]) -> str:\n"
        '    job_id = str(job_info.get("job_id") or "")\n'
        '    current = str(job_info.get("elb_job_id") or "")\n'
        '    if current.startswith("job-"):\n'
        "        return current\n"
        "    discovered = _discover_elb_job_id_from_submit_output(\n"
        "        job_id,\n"
        '        "\\n".join(str(job_info.get(key) or "") for key in '
        '("stdout_tail", "stderr_tail")),\n'
        "    )\n"
        "    if discovered:\n"
        "        _update_job(job_id, elb_job_id=discovered)\n"
        "        return discovered\n"
        "    return current or job_id\n"
        "\n\n"
        "def next_helper() -> None:\n"
        "    pass\n"
    )

    module._harden_openapi_runtime_ids(path)
    first = path.read_text()
    module._harden_openapi_runtime_ids(path)
    assert path.read_text() == first

    updates: list[tuple[str, str]] = []

    def _update_job(job_id: str, *, elb_job_id: str) -> None:
        updates.append((job_id, elb_job_id))

    namespace: dict[str, Any] = {"Any": Any, "re": re, "_update_job": _update_job}
    exec(first, namespace)  # noqa: S102 - execute only generated temporary fixture code.
    canonical_upper = "job-" + "A" * 32
    canonical_lower = canonical_upper.lower()
    discover = namespace["_discover_elb_job_id_from_submit_output"]
    effective = namespace["_effective_elb_job_id"]

    assert discover("request-1", f"/results/request-1/{canonical_upper}/metadata/") == (
        canonical_lower
    )
    assert discover("request-1", "/results/request-1/job-not-canonical/metadata/") == ""
    assert effective({"job_id": "request-1", "elb_job_id": canonical_upper}) == (canonical_lower)
    assert effective({"job_id": "request-1", "elb_job_id": "job-not-canonical"}) == ("request-1")
    assert effective({"job_id": "request-1", "stdout_tail": canonical_upper}) == (canonical_lower)
    assert updates == [("request-1", canonical_lower)]


def test_patch_app_rejects_duplicate_runtime_id_helpers(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def _discover_elb_job_id_from_submit_output(job_id, stdout):\n"
        '    return ""\n\n'
        "def _effective_elb_job_id(job_info):\n"
        '    return ""\n\n'
        "def _effective_elb_job_id(job_info):\n"
        '    return ""\n\n'
        "def next_helper():\n"
        "    pass\n"
    )

    with pytest.raises(RuntimeError, match="exactly one OpenAPI effective"):
        module._harden_openapi_runtime_ids(path)


def test_patch_app_hardens_all_runtime_id_consumers(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        'if runtime_job_id.startswith("job-") and runtime_job_id != job_id:\n'
        "    pass\n"
        'if elb_job_id.startswith("job-"):\n'
        "    pass\n"
        'if effective_elb_job_id.startswith("job-") and '
        'job_info.get("elb_job_id") != effective_elb_job_id:\n'
        "    pass\n"
        'if effective_elb_job_id.startswith("job-") and effective_elb_job_id != str(\n'
        '    job_info.get("job_id") or ""\n'
        "):\n"
        "    pass\n"
    )

    module._harden_openapi_runtime_id_consumers(path)
    text = path.read_text()

    assert '.startswith("job-")' not in text
    assert text.count('re.fullmatch(r"job-[0-9a-f]{32}"') == 4


def test_patch_submit_runtime_id_rejects_noncanonical_correlation(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def submit(job_id, payload, result, status):\n"
        "        _update_job(\n"
        "            job_id,\n"
        "            status=status,\n"
        '            phase="submitted" if status == "running" else status,\n'
        "            elb_job_id=(\n"
        '                payload.get("correlation_id")\n'
        '                or _discover_elb_job_id_from_submit_output(job_id, result.stdout or "")\n'
        "                or job_id\n"
        "            ),\n"
        "        )\n"
    )

    module._patch_submit_runtime_id_priority(path)
    first = path.read_text()
    module._patch_submit_runtime_id_priority(path)

    assert path.read_text() == first
    updates: list[str] = []

    def _update_job(_job_id: str, **kwargs: Any) -> None:
        updates.append(kwargs["elb_job_id"])

    canonical = "job-" + "a" * 32
    namespace: dict[str, Any] = {
        "re": re,
        "_update_job": _update_job,
        "_discover_elb_job_id_from_submit_output": (
            lambda _job_id, stdout: canonical if stdout == "discover" else ""
        ),
    }
    exec(first, namespace)  # noqa: S102 - execute only generated temporary fixture code.
    submit = namespace["submit"]
    submit(
        "request-1",
        {"correlation_id": "wf3:request-1"},
        SimpleNamespace(stdout="discover"),
        "running",
    )
    submit(
        "request-2",
        {"correlation_id": canonical.upper()},
        SimpleNamespace(stdout="other"),
        "running",
    )
    submit("request-3", {}, SimpleNamespace(stdout="other"), "running")

    assert updates == [canonical, canonical, "request-3"]


def test_replace_once_removes_block_idempotently(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text("before\nunsafe fallback\nafter\n")

    module._replace_once(path, "unsafe fallback\n", "")
    first = path.read_text()
    module._replace_once(path, "unsafe fallback\n", "")

    assert first == "before\nafter\n"
    assert path.read_text() == first


def test_replace_once_unless_marker_accepts_later_hardening(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text("legacy\n")

    module._replace_once_unless_marker(path, "legacy\n", "modern permissive\n", "modern")
    path.write_text(path.read_text().replace("permissive", "strict"))
    module._replace_once_unless_marker(path, "legacy\n", "modern permissive\n", "modern")

    assert path.read_text() == "modern strict\n"


def test_dockerfile_validator_rejects_tampered_identity_assertion(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("stub\n")
    (tmp_path / "Dockerfile").write_text(
        "ARG ELB_REF=old\n"
        "COPY ./app /app\n"
        "RUN true && \\\n"
        "    git -C /tmp/elb-src checkout ${ELB_REF} && \\\n"
        "    rm -rf /tmp/elb-src && \\\n"
        "    pip3 install --no-cache-dir --no-build-isolation /tmp/elb-src && \\\n"
        "    true\n"
        "RUN true \\\n"
        "    && pip install --no-cache-dir azure-cli \\\n"
        "    && true\n"
    )
    module.patch_dockerfile(tmp_path)
    path = tmp_path / "Dockerfile"
    path.write_text(path.read_text().replace("|| exit 1; done &&", "|| true; done &&", 1))

    import pytest

    with pytest.raises(RuntimeError, match="runtime policy mismatch"):
        module._validate_dockerfile_runtime_policy(path)
