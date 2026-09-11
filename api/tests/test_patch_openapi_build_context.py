"""Tests for the OpenAPI image build-context patcher.

Responsibility: Verify OpenAPI image patching enforces runtime policy and refreshes stale
ElasticBLAST scripts.
Edit boundaries: Use temporary build contexts only; never invoke Docker or Azure.
Key entry points: `test_patch_dockerfile_asserts_ttl_in_all_runtime_copies`,
`test_patch_removes_obsolete_precise_search_space_guard`,
`test_patch_app_reconciles_elb_scripts_by_content`,
`test_patch_allows_only_canonical_merged_result_through_blob_path_guard`,
`test_patch_submit_runtime_id_rejects_noncanonical_correlation`,
`test_patched_reference_context_route_enforces_auth_and_response_model`.
Risky contracts: The assertions must cover source, system Python, and venv templates; OpenAPI
submits must never trust historical warmup Jobs or name-only ConfigMap checks as node-local
cache-presence proof. Missing precise search space must reach the active-generation fallback. Result
path guards must continue rejecting traversal and arbitrary files.
Validation: `uv run pytest -q api/tests/test_patch_openapi_build_context.py`.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import sys
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


def test_copy_app_overlay_includes_runtime_modules(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / "app").mkdir()

    module._copy_app_overlay(tmp_path)

    assert (tmp_path / "app" / "eta.py").is_file()
    exact = tmp_path / "app" / "exact_oracle.py"
    assert exact.is_file()
    assert "def attach_db_order_oracle(" in exact.read_text()
    reference = tmp_path / "app" / "reference_context.py"
    assert reference.is_file()
    assert "def resolve_reference_context(" in reference.read_text()


def test_copy_app_overlay_preserves_tracked_sibling_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    app = tmp_path / "app"
    app.mkdir()
    (app / "exact_oracle.py").write_text("# sibling-native exact oracle\n")
    (app / "reference_context.py").write_text("# sibling-native resolver\n")
    monkeypatch.setattr(
        module,
        "_is_tracked_sibling_module",
        lambda _root, path: path.name in {"exact_oracle.py", "reference_context.py"},
    )

    module._copy_app_overlay(tmp_path)

    assert (app / "exact_oracle.py").read_text() == "# sibling-native exact oracle\n"
    assert (app / "reference_context.py").read_text() == "# sibling-native resolver\n"
    assert (app / "eta.py").is_file()


def test_validate_copied_runtime_policy_rejects_missing_marker(tmp_path: Path) -> None:
    module = _load_module()
    app = tmp_path / "app"
    app.mkdir()
    (app / "exact_oracle.py").write_text("def _context_nonnegative_int():\n    pass\n")
    (app / "reference_context.py").write_text(
        "from defusedxml import ElementTree as ET\n"
        "active_total_letters,\n"
        "deepcopy(cached[1])\n"
        "_FETCH_LOCK.acquire(timeout=_FETCH_LOCK_WAIT_SECONDS)\n"
    )
    (app / "requirements.txt").write_text("defusedxml==0.7.1\n")
    (tmp_path / "merge-sharded-results.sh").write_text(
        "num_shards must be between 1 and 1024\n"
    )

    with pytest.raises(RuntimeError, match="volume limit"):
        module._validate_copied_runtime_policy(tmp_path)


def test_validate_copied_runtime_policy_rejects_legacy_candidate_cap(
    tmp_path: Path,
) -> None:
    module = _load_module()
    app = tmp_path / "app"
    app.mkdir()
    (app / "result_selection.py").write_text(
        "SEQUENCE_DIVERSITY_DEFAULT_CANDIDATE_POOL_SIZE = 2_000\n"
        "SEQUENCE_DIVERSITY_MAX_CANDIDATE_POOL_SIZE = 5_000\n"
        "if applied_pool <= 0:\n"
        "    pass\n"
        "def prepare_sequence_diversity_options():\n"
        "    pass\n"
    )
    (app / "exact_oracle.py").write_text(
        "def _context_nonnegative_int():\n"
        "    pass\n"
        "One-shard manifest exceeds the volume limit\n"
        "oracle_source != source_version\n"
    )
    (app / "reference_context.py").write_text(
        "from defusedxml import ElementTree as ET\n"
        "active_total_letters,\n"
        "deepcopy(cached[1])\n"
        "_FETCH_LOCK.acquire(timeout=_FETCH_LOCK_WAIT_SECONDS)\n"
    )
    (app / "requirements.txt").write_text("defusedxml==0.7.1\n")
    (tmp_path / "merge-sharded-results.sh").write_text(
        "num_shards must be between 1 and 1024\n"
        "SEQUENCE_GROUP_REPORT_LIMIT = 5_000\n"
        "def sequence_diversity_representatives():\n"
        "    pass\n"
        "len(observed_source_shards) == expected_shards\n"
    )

    with pytest.raises(
        RuntimeError,
        match="forbidden SEQUENCE_DIVERSITY_MAX_CANDIDATE_POOL_SIZE",
    ):
        module._validate_copied_runtime_policy(tmp_path)


@pytest.mark.parametrize(
    "existing",
    [
        "fastapi\nrequests>=2.31.0\n",
        "fastapi\ndefusedxml>=0.6\nrequests>=2.31.0\n",
    ],
)
def test_reference_context_dependency_is_pinned_once(
    tmp_path: Path,
    existing: str,
) -> None:
    module = _load_module()
    app = tmp_path / "app"
    app.mkdir()
    requirements = app / "requirements.txt"
    requirements.write_text(existing)

    module._ensure_reference_context_dependency(tmp_path)
    first = requirements.read_text()
    module._ensure_reference_context_dependency(tmp_path)

    assert requirements.read_text() == first
    assert first.count("defusedxml==0.7.1") == 1
    assert "defusedxml>=0.6" not in first


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
        "class BlastOptions(BaseModel):\n"
        "    extra: Optional[str] = Field(None, "
        'description="Additional BLAST CLI options as raw string.")\n\n'
        "class ExternalBlastOptions(BaseModel):\n"
        "    dust: bool = Field(True)\n"
    )
    main = app / "main.py"
    main.write_text(
        "def _build_options(opts):\n"
        "    parts = []\n"
        "    if opts:\n"
        "        if opts.extra: parts.append(opts.extra)\n"
        "    return parts\n\n"
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
    assert "class BlastOptions(BaseModel):" in first_schema
    assert first_schema.count("db_effective_search_space: Optional[int]") == 2
    assert '"native_top_n", "diversity_aware", "sequence_diversity"' in first_schema
    assert 'web_blast_statistical_context: Optional["WebBlastStatisticalContext"]' in first_schema
    assert "db_effective_search_space: Optional[int] = Field(None, ge=1)" in first_schema
    assert "class WebBlastStatisticalContext(BaseModel):" in first_schema
    assert '"-soft_masking false"' in first_main
    assert "req.options.soft_masking" in first_main
    assert 'parts.append("-searchsp " + str(opts.db_effective_search_space))' in first_main
    assert 'f" -searchsp {req.options.db_effective_search_space}"' in first_main
    assert "opts.web_blast_statistical_context.filtered_database_letters" in first_main
    assert "web_blast_statistical_context=(" in first_main
    assert 'parts.append(f"-searchsp {opts.db_effective_search_space}")' in first_main
    assert "blast_options.extra -searchsp/-dbsize" in first_main
    ast.parse(first_schema)
    ast.parse(first_main)

    class FakeHTTPException(Exception):
        pass

    namespace: dict[str, Any] = {"HTTPException": FakeHTTPException, "re": re}
    exec(first_main, namespace)  # noqa: S102 - generated temporary fixture code.
    build_options = namespace["_build_options"]
    assert build_options(SimpleNamespace(extra="", db_effective_search_space=123)) == [
        "-searchsp 123"
    ]
    with pytest.raises(FakeHTTPException):
        build_options(
            SimpleNamespace(
                extra="-dbsize 123",
                db_effective_search_space=456,
            )
        )


def test_patch_publishes_reference_and_job_response_schemas(tmp_path: Path) -> None:
    module = _load_module()
    app = tmp_path / "app"
    app.mkdir()
    schemas = app / "schemas.py"
    schemas.write_text(
        "from typing import Any, Literal, Optional\n"
        "from pydantic import BaseModel, Field\n\n"
        "class WebBlastStatisticalContext(BaseModel):\n"
        "    filtered_database_letters: int\n"
        "    length_adjustment: int = Field(..., ge=0)\n\n"
        "class ExternalBlastOptions(BaseModel):\n"
        "    pass\n"
    )
    main = app / "main.py"
    main.write_text(
        "from schemas import (\n"
        "    JobSubmitRequest,\n"
        ")\n"
        "from util import run_cancellable, safe_exec\n\n"
        "# ── Jobs — Submit ──────────────────────────────────────────────────────────\n"
        '@v1.post("/jobs", tags=["Jobs"], status_code=202, summary="Submit a BLAST search",\n'
        "          openapi_extra={})\n"
        "def submit_job(req):\n"
        "    return {}\n\n"
        '@v1.get("/jobs", tags=["Jobs"], summary="List all jobs")\n'
        "async def list_jobs():\n"
        "    return {}\n\n"
        '@v1.get("/jobs/{job_id}/status", tags=["Jobs"], summary="Get job status")\n'
        "async def get_job_status(job_id):\n"
        "    return {}\n\n"
        '@external_v1.post("/submit", status_code=202, '
        'summary="Submit an external ElasticBLAST job")\n'
        "def external_submit(req):\n"
        "    return {}\n\n"
        '@external_v1.get("/jobs/{job_id}", '
        'summary="Get external ElasticBLAST job status")\n'
        "async def external_job_status(job_id):\n"
        "    return {}\n"
    )

    module._patch_openapi_response_schemas(tmp_path)
    module._patch_reference_context_endpoint(tmp_path)
    first_schema = schemas.read_text()
    first_main = main.read_text()
    module._patch_openapi_response_schemas(tmp_path)
    module._patch_reference_context_endpoint(tmp_path)

    assert schemas.read_text() == first_schema
    assert main.read_text() == first_main
    assert "class WebBlastStatisticalContextRequest(BaseModel):" in first_schema
    assert "length_adjustment: int = Field(..., ge=0)" in first_schema
    assert 'pattern=r"^[A-Z0-9]{8,16}$"' in first_schema
    assert "omitted defaults to true" in first_schema
    assert "class WebBlastStatisticalContextResponse(BaseModel):" in first_schema
    assert "class JobStatusResponse(BaseModel):" in first_schema
    assert "class JobListResponse(BaseModel):" in first_schema
    assert "results_ready: Optional[bool] = None" in first_schema
    assert "merged_at: Optional[str] = None" in first_schema
    assert 'Literal["native_top_n", "diversity_aware", "sequence_diversity"]' in first_schema
    assert "sequence_identity_mode" in first_schema
    assert "candidate_pool_size_applied_per_shard" in first_schema
    assert '"/web-blast/statistical-context"' in first_main
    assert "response_model=WebBlastStatisticalContextResponse" in first_main
    assert "response_model=JobListResponse" in first_main
    assert first_main.count("response_model=JobStatusResponse") == 3
    assert "import reference_context as _reference_context" in first_main
    ast.parse(first_schema)
    ast.parse(first_main)


def test_patched_reference_context_route_enforces_auth_and_response_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _load_module()
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "schemas.py").write_text(
        "from typing import Any, Literal, Optional\n"
        "from pydantic import BaseModel, Field\n\n"
        "class JobSubmitRequest(BaseModel):\n"
        "    pass\n\n"
        "class WebBlastStatisticalContext(BaseModel):\n"
        "    filtered_database_letters: int\n"
        "    filtered_database_sequences: int\n"
        "    length_adjustment: int\n"
        "    effective_search_space: int\n"
        "    scoring_search_space: int\n"
        "    result_database_letters: int\n\n"
        "class WebBlastStatisticalContextRequest(BaseModel):\n"
        '    rid: str = Field(..., pattern=r"^[A-Z0-9]{8,16}$")\n'
        "    query_fasta: str\n"
        '    db: Literal["core_nt"] = "core_nt"\n'
        "    taxid: Optional[int] = None\n"
        "    is_inclusive: Optional[bool] = None\n\n"
        "class WebBlastStatisticalContextResponse(BaseModel):\n"
        '    status: Literal["resolved"]\n'
        "    rid: str\n"
        '    database: Literal["core_nt"]\n'
        "    reference_query_id: str\n"
        "    submitted_query_id: str\n"
        "    query_length: int\n"
        "    active_source_version: str\n"
        "    web_blast_statistical_context: WebBlastStatisticalContext\n"
        "    query_effective_search_spaces: list[int]\n"
        "    expected_filter: dict[str, Any]\n"
        "    evidence: dict[str, Any]\n"
        "    warnings: list[str]\n"
    )
    (app_dir / "util.py").write_text(
        "def run_cancellable(*_args, **_kwargs):\n"
        "    return None\n\n"
        "def safe_exec(*_args, **_kwargs):\n"
        "    return None\n"
    )
    (app_dir / "reference_context.py").write_text(
        "class ReferenceContextError(ValueError):\n"
        "    pass\n\n"
        "class ReferenceContextNotReady(ReferenceContextError):\n"
        "    pass\n\n"
        "class ReferenceContextUnavailable(RuntimeError):\n"
        "    pass\n\n"
        "def resolve_reference_context(**_kwargs):\n"
        "    raise AssertionError('resolver must be replaced by the test')\n"
    )
    main = app_dir / "main.py"
    main.write_text(
        "import logging\n"
        "from typing import Any, Optional\n"
        "from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException\n"
        "from schemas import (\n"
        "    JobSubmitRequest,\n"
        "    WebBlastStatisticalContextRequest,\n"
        "    WebBlastStatisticalContextResponse,\n"
        ")\n"
        "from util import run_cancellable, safe_exec\n\n"
        'logger = logging.getLogger("test-openapi")\n'
        "app = FastAPI()\n"
        '_API_TOKEN = "test-token"\n\n'
        "def require_api_token(\n"
        '    token: Optional[str] = Header(None, alias="X-ELB-API-Token"),\n'
        ") -> None:\n"
        "    if token != _API_TOKEN:\n"
        '        raise HTTPException(401, "missing or invalid token")\n\n'
        'v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_token)])\n'
        "_exact_oracle = None\n\n"
        "def _blob_base() -> str:\n"
        '    return "https://example.invalid/container"\n\n'
        "def _storage_oauth_token() -> str:\n"
        '    return "mock-token"\n\n'
        "# ── Jobs — Submit ──────────────────────────────────────────────────────────\n"
        "app.include_router(v1)\n"
    )

    module._patch_reference_context_endpoint(tmp_path)
    monkeypatch.syspath_prepend(str(app_dir))
    for name in ("schemas", "util", "reference_context", "patched_openapi_main"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location("patched_openapi_main", main)
    assert spec is not None and spec.loader is not None
    generated = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, generated)
    spec.loader.exec_module(generated)

    generated._exact_oracle = SimpleNamespace(
        read_active_database=lambda **_kwargs: SimpleNamespace(
            total_letters=1000,
            total_sequences=100,
            source_version="test-generation",
        )
    )
    resolver_calls: list[dict[str, Any]] = []

    def resolve_reference_context(**kwargs: Any) -> dict[str, Any]:
        resolver_calls.append(kwargs)
        return {
            "status": "resolved",
            "rid": "ABCDEFGH",
            "database": "core_nt",
            "reference_query_id": "q1",
            "submitted_query_id": "q1",
            "query_length": 4,
            "active_source_version": "test-generation",
            "web_blast_statistical_context": {
                "filtered_database_letters": 1000,
                "filtered_database_sequences": 100,
                "length_adjustment": 1,
                "effective_search_space": 2700,
                "scoring_search_space": 3000,
                "result_database_letters": 1000,
            },
            "query_effective_search_spaces": [2700],
            "expected_filter": {
                "taxid": 1,
                "is_inclusive": True,
                "verified_from_result": False,
            },
            "evidence": {"source": "mock"},
            "warnings": [],
            "internal_only": "must be removed by the response model",
        }

    monkeypatch.setattr(
        generated._reference_context,
        "resolve_reference_context",
        resolve_reference_context,
    )

    from fastapi.testclient import TestClient

    client = TestClient(generated.app)
    payload = {
        "rid": "ABCDEFGH",
        "query_fasta": ">q1\nACGT\n",
        "db": "core_nt",
        "taxid": 1,
    }
    unauthenticated = client.post("/v1/web-blast/statistical-context", json=payload)
    authenticated = client.post(
        "/v1/web-blast/statistical-context",
        json=payload,
        headers={"X-ELB-API-Token": "test-token"},
    )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json()["web_blast_statistical_context"][
        "effective_search_space"
    ] == 2700
    assert "internal_only" not in authenticated.json()
    assert resolver_calls == [
        {
            "rid": "ABCDEFGH",
            "query_fasta": ">q1\nACGT\n",
            "active_total_letters": 1000,
            "active_total_sequences": 100,
            "active_source_version": "test-generation",
            "taxid": 1,
            "is_inclusive": True,
        }
    ]

    def fail_with(error: Exception):
        def fail_resolver(**_kwargs: Any) -> None:
            raise error

        return fail_resolver

    error_cases = (
        (
            generated._reference_context.ReferenceContextNotReady("not ready"),
            409,
            "reference_not_ready",
            "30",
        ),
        (
            generated._reference_context.ReferenceContextUnavailable("unavailable"),
            503,
            "reference_unavailable",
            None,
        ),
        (
            generated._reference_context.ReferenceContextError("invalid"),
            422,
            "reference_invalid",
            None,
        ),
    )
    for error, status_code, error_code, retry_after in error_cases:
        monkeypatch.setattr(
            generated._reference_context,
            "resolve_reference_context",
            fail_with(error),
        )
        failed = client.post(
            "/v1/web-blast/statistical-context",
            json=payload,
            headers={"X-ELB-API-Token": "test-token"},
        )
        assert failed.status_code == status_code
        assert failed.json()["detail"]["code"] == error_code
        assert failed.headers.get("Retry-After") == retry_after

    def fail_active_database(**_kwargs: Any) -> None:
        raise RuntimeError("sensitive storage detail")

    generated._exact_oracle = SimpleNamespace(
        read_active_database=fail_active_database,
    )
    unavailable = client.post(
        "/v1/web-blast/statistical-context",
        json=payload,
        headers={"X-ELB-API-Token": "test-token"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "detail": {
            "code": "active_database_unavailable",
            "message": "Active database generation metadata is unavailable",
            "retryable": True,
        }
    }
    assert "sensitive storage detail" not in unavailable.text
    assert "active database metadata unavailable error_type=RuntimeError" in caplog.text
    assert "sensitive storage detail" not in caplog.text


def test_patch_upgrades_existing_reference_context_route(tmp_path: Path) -> None:
    module = _load_module()
    app = tmp_path / "app"
    app.mkdir()
    main = app / "main.py"
    main.write_text(
        "from util import run_cancellable, safe_exec\n\n"
        "def resolve_web_blast_statistical_context(req):\n"
        "    try:\n"
        "        active_database = _exact_oracle.read_active_database(\n"
        "            blob_base=_blob_base(),\n"
        "            db_name=req.db,\n"
        "            token=_storage_oauth_token(),\n"
        "        )\n"
        "        return _reference_context.resolve_reference_context(\n"
        "            rid=req.rid,\n"
        "        )\n"
        "    except _reference_context.ReferenceContextError as exc:\n"
        "        raise RuntimeError from exc\n"
    )

    module._patch_reference_context_endpoint(tmp_path)
    first = main.read_text()
    module._patch_reference_context_endpoint(tmp_path)

    assert main.read_text() == first
    assert first.count('"active_database_unavailable"') == 1
    assert first.count("active database metadata unavailable error_type=%s") == 1
    assert first.count("import reference_context as _reference_context") == 1
    ast.parse(first)


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
        '    for name in job_info.get("listed_names", []):\n'
        '        if not name.startswith("batch_"):\n'
        "            continue\n"
        '        files.append({"filename": name})\n'
        "    return files\n"
    )

    module._patch_canonical_merged_result_discovery(path)
    first = path.read_text()
    module._patch_canonical_merged_result_discovery(path)

    assert path.read_text() == first
    ast.parse(first)
    assert "def _result_partition_count(job_info):" in first
    assert 'item.get("filename") == "merged_results.out.gz"' in first
    assert "requires_merged_result" in first
    assert 'if name == "merged_results.out.gz":' in first
    assert "files = []" in first
    assert "seen = set()" in first

    namespace: dict[str, Any] = {}
    exec(first, namespace)  # noqa: S102 - generated temporary fixture code.
    list_result_files = namespace["_list_result_files"]
    shard = {"filename": "batch_1.out.gz"}
    merged = {"filename": "merged_results.out.gz"}

    assert list_result_files({"result_files": [shard]}) == [shard]
    assert (
        list_result_files(
            {
                "exact_oracle": {"db_partitions": 10},
                "result_files": [shard],
                "listed_names": ["batch_1.out.gz"],
            }
        )
        == []
    )
    assert list_result_files(
        {
            "exact_oracle": {"db_partitions": 10},
            "result_files": [shard],
            "listed_names": ["batch_1.out.gz", "merged_results.out.gz"],
        }
    ) == [merged]


def test_patch_partitioned_completion_requires_marker_and_merge(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "RESULTS_VISIBILITY_GRACE_SECONDS = max(0, int(os.environ.get("
        '"ELB_OPENAPI_RESULTS_VISIBILITY_GRACE_SECONDS", "120")))\n'
        "\n"
        "def _result_partition_count(job_info):\n"
        '    return int(job_info.get("db_partitions", 0) or 0)\n'
        "\n"
        "def _refresh_job_status(job_id):\n"
        "    with _jobs_lock:\n"
        "        job = dict(_jobs.get(job_id, {}))\n"
        "    if not job:\n"
        "        return None\n"
        '    if job.get("status") in _TERMINAL_STATES or job.get("status") == "queued":\n'
        "        return job\n"
        "\n"
        "    elb_job_id = _effective_elb_job_id(job)\n"
        '    marker_results_url = str(job.get("results", "")).rstrip("/")\n'
        "    marker = _job_marker_phase(marker_results_url)\n"
        '    if marker == "failed":\n'
        '        return _update_job(job_id, status="failed", phase="failed")\n'
        '    if marker == "completed":\n'
        "        if _list_result_files(job):\n"
        "            updates = {\n"
        '                "status": "completed",\n'
        '                "phase": "completed",\n'
        '                "completed_at": _now_iso(),\n'
        '                "last_progress_at": _now_iso(),\n'
        "            }\n"
        "            summary_snapshot = _snapshot_k8s_summary_for_terminal(job, elb_job_id)\n"
        "            if summary_snapshot is not None:\n"
        '                updates["k8s_summary"] = summary_snapshot\n'
        "            result = _update_job(job_id, **updates)\n"
        "            _notify_terminal_transition(job_id, updates)\n"
        "            return result\n"
        '        seen_at = job.get("success_marker_seen_at") or _now_iso()\n'
        "        if _age_seconds(seen_at) > RESULTS_VISIBILITY_GRACE_SECONDS:\n"
        "            updates = {\n"
        '                "status": "completed",\n'
        '                "phase": "completed",\n'
        '                "completed_at": _now_iso(),\n'
        '                "last_progress_at": _now_iso(),\n'
        "            }\n"
        "            summary_snapshot = _snapshot_k8s_summary_for_terminal(job, elb_job_id)\n"
        "            if summary_snapshot is not None:\n"
        '                updates["k8s_summary"] = summary_snapshot\n'
        "            result = _update_job(job_id, **updates)\n"
        "            _notify_terminal_transition(job_id, updates)\n"
        "            return result\n"
        "        return _update_job(\n"
        '            job_id, status="running", phase="finalizing",\n'
        "            success_marker_seen_at=seen_at, last_progress_at=_now_iso(),\n"
        "        )\n"
        "\n"
        "    summary = _k8s_job_summary(elb_job_id)\n"
        "    stuck_reason = _k8s_pod_stuck_reason(elb_job_id)\n"
        '    updates = {"k8s_summary": summary}\n'
        "    if stuck_reason:\n"
        '        return _update_job(job_id, status="failed", phase="stuck_cancelled")\n'
        '    if summary.get("finalizer_failed_terminal"):\n'
        '        updates.update({"status": "failed", "phase": "finalizer_failed"})\n'
        '    elif summary.get("submit_failed_terminal"):\n'
        '        updates.update({"status": "failed", "phase": "submit_failed"})\n'
        '    elif summary.get("failed_terminal"):\n'
        '        updates.update({"status": "failed", "phase": "blast_failed"})\n'
        '    elif summary.get("total", 0) > 0:\n'
        '        if summary.get("succeeded", 0) >= summary.get("total", 0) '
        'and summary.get("total", 0) > 0:\n'
        "            if _list_result_files(job):\n"
        '                updates.update({"status": "completed", "phase": "completed", '
        '"completed_at": _now_iso()})\n'
        "            else:\n"
        '                updates.update({"status": "running", "phase": "finalizing"})\n'
        '        elif summary.get("active", 0) > 0:\n'
        '            updates.update({"status": "running", "phase": "running"})\n'
        "        else:\n"
        '            updates.update({"status": "running", "phase": "pending"})\n'
        "    else:\n"
        "        if _list_result_files(job):\n"
        '            updates.update({"status": "completed", "phase": "completed", '
        '"completed_at": _now_iso()})\n'
        "        else:\n"
        '            updates.update({"phase": "submitting"})\n'
        "    return _update_job(job_id, **updates)\n"
    )

    module._patch_partitioned_completion_fail_closed(path)
    first = path.read_text()
    module._patch_partitioned_completion_fail_closed(path)

    assert path.read_text() == first
    ast.parse(first)
    assert "PARTITIONED_RESULT_FINALIZER_DEADLINE_SECONDS" in first
    assert "requires_canonical_merge = _result_partition_count(job) > 1" in first
    assert '"phase": "finalizer_failed"' in first

    state: dict[str, Any] = {
        "age": 121,
        "files": [],
        "marker": "completed",
        "summary": {},
    }
    jobs = {
        "partitioned": {"status": "running", "db_partitions": 10, "results": "r"},
        "single": {"status": "running", "db_partitions": 1, "results": "r"},
    }

    def update(job_id: str, **updates: Any) -> dict[str, Any]:
        jobs[job_id].update(updates)
        return dict(jobs[job_id])

    namespace: dict[str, Any] = {
        "os": __import__("os"),
        "_jobs": jobs,
        "_jobs_lock": __import__("contextlib").nullcontext(),
        "_TERMINAL_STATES": {"completed", "failed"},
        "_effective_elb_job_id": lambda job: str(job.get("job_id") or "runtime"),
        "_job_marker_phase": lambda _url: state["marker"],
        "_list_result_files": lambda _job: state["files"],
        "_age_seconds": lambda _seen: state["age"],
        "_now_iso": lambda: "now",
        "_snapshot_k8s_summary_for_terminal": lambda *_args: None,
        "_update_job": update,
        "_notify_terminal_transition": lambda *_args: None,
        "_k8s_job_summary": lambda _job_id: state["summary"],
        "_k8s_pod_stuck_reason": lambda _job_id: None,
    }
    exec(first, namespace)  # noqa: S102 - generated temporary fixture code.
    refresh = namespace["_refresh_job_status"]

    assert refresh("partitioned")["phase"] == "finalizing"
    assert refresh("single")["status"] == "completed"

    jobs["partitioned"].update(status="running", phase="finalizing")
    state["age"] = 1801
    failed = refresh("partitioned")
    assert failed["status"] == "failed"
    assert failed["phase"] == "finalizer_failed"

    jobs["partitioned"].update(status="running", phase="running")
    state.update(
        marker=None,
        files=[{"filename": "merged_results.out.gz"}],
        summary={"total": 10, "succeeded": 10},
    )
    held = refresh("partitioned")
    assert held["status"] == "running"
    assert held["phase"] == "finalizing"


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


def test_patch_database_detail_uses_active_generation(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def get_blast_database(db_name, response):\n"
        "    safe = db_name\n"
        "    meta = {'snapshot': 'legacy', 'number_of_sequences': 1, 'number_of_letters': 2}\n"
        "    cache_status = 'HIT'\n"
        "    if not meta:\n"
        "        raise HTTPException(404, 'missing')\n"
        '    response.headers["X-Cache"] = cache_status\n'
        "    return meta\n"
    )

    module._patch_active_database_metadata(path)
    first = path.read_text()
    module._patch_active_database_metadata(path)

    assert path.read_text() == first
    assert '"snapshot": active_database.source_version' in first
    assert '"number_of_sequences": active_database.total_sequences' in first
    assert '"number_of_letters": active_database.total_letters' in first
    ast.parse(first)


def test_patch_status_payloads_expose_result_readiness(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def _external_job_payload(job_info):\n"
        "    public_status = 'success'\n"
        "    payload = {}\n"
        '    elif public_status == "success":\n'
        '        payload["completed_at"] = job_info.get("completed_at") or '
        'job_info.get("updated_at", "")\n'
        "        files = _list_result_files(job_info)\n"
        '        result_payload: dict[str, Any] = {"files": files}\n'
        '        if "hit_count" in job_info:\n'
        '            result_payload["hit_count"] = int(job_info.get("hit_count", 0) or 0)\n'
        '        payload["result"] = result_payload\n'
        "\n"
        "def get_job_status(job_info):\n"
        "    _status_payload = {\n"
        '        "kubernetes": {"summary": job_info.get("k8s_summary", {})},\n'
        "    }\n"
        '    _pt = job_info.get("passthrough")\n'
        "    return _status_payload\n"
    )

    module._patch_result_readiness_payloads(path)
    first = path.read_text()
    module._patch_result_readiness_payloads(path)

    assert path.read_text() == first
    assert 'payload["results_ready"] = bool(files)' in first
    assert '_status_payload["results_ready"] = bool(status_files)' in first
    assert first.count('["merged_at"] = ready_at') == 2
    status_ready = first.index('_status_payload["results_ready"]')
    assert status_ready < first.index("    return _status_payload\n", status_ready)


def test_patch_normalizes_unreachable_status_tail(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    eta_block = (
        '    if _eta is not None and _eta.enabled() and job_info.get("status") '
        'in {"queued", "dispatching", "submitting", "running"}:\n'
        "        with _jobs_lock:\n"
        "            _eta_jobs = [dict(v) for v in _jobs.values()]\n"
        "        _eta_out = _eta.compute_eta(job_info, _eta_jobs, MAX_ACTIVE_SUBMISSIONS)\n"
        "        if _eta_out:\n"
        '            _status_payload["eta"] = _eta_out\n'
    )
    path.write_text(
        "def get_job_status(job_info):\n"
        "    _status_payload = {}\n"
        + eta_block
        + "    return _status_payload\n"
        + '    _pt = job_info.get("passthrough")\n'
        + "    if isinstance(_pt, dict) and _pt:\n"
        + '        _status_payload["passthrough"] = _pt\n'
        + eta_block
        + "    return _status_payload\n"
    )

    module._normalize_status_payload_tail(path)
    first = path.read_text()
    module._normalize_status_payload_tail(path)

    assert path.read_text() == first
    assert first.count("    return _status_payload\n") == 1
    assert first.index('_status_payload["passthrough"]') < first.index(
        "    return _status_payload\n"
    )
    ast.parse(first)


def test_patch_result_selection_policy_controls_oracle_and_provenance(
    tmp_path: Path,
) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def submit(req, config, job_data, passthrough, payload, job_info):\n"
        "    is_b = req.query_fasta is not None\n"
        "    web_blast_statistics = None\n"
        "    value = prepare(\n"
        '                context=(req.model_extra or {}).get("web_blast_statistical_context"),\n'
        "    )\n"
        "    if req.batch_len is not None:\n"
        '        config["blast"]["batch-len"] = str(req.batch_len)\n'
        "    exact_oracle_info = None\n"
        '    if db_name == "core_nt" and profile in '
        '{"core_nt_precise", "precise", "core_nt_safe"}:\n'
        "        db_version = {\n"
        '            "version": active_database.source_version,\n'
        "        }\n"
        "    if passthrough:\n"
        '        job_data["passthrough"] = passthrough\n'
        "    _pt = job_info.get('passthrough')\n"
        "    if isinstance(_pt, dict) and _pt:\n"
        '        payload["passthrough"] = _pt\n'
        '    for _runtime_key in ("exact_oracle", "web_blast_statistics"):\n'
        "        pass\n"
    )

    module._patch_result_selection_policy(path)
    first = path.read_text()
    module._patch_result_selection_policy(path)

    assert path.read_text() == first
    assert "req.blast_options.result_selection_policy" in first
    assert "req.blast_options.web_blast_statistical_context.model_dump()" in first
    assert 'else (req.model_extra or {}).get("web_blast_statistical_context")' in first
    assert 'and web_blast_context not in (None, "")' in first
    assert "context=web_blast_context" in first
    assert "web_blast_statistical_context requires native_top_n result selection" in first
    assert 'config["blast"]["result-selection-policy"] = selection_policy' in first
    assert 'and selection_policy == "native_top_n"' in first
    assert 'job_data["result_selection_policy"] = selection_policy' in first
    assert 'job_data["db_partitions"] = int(' in first
    assert 'payload["result_selection_policy"] = job_info.get(' in first
    assert "Keep diversity-aware runs on the same active DB provenance" in first
    ast.parse(first)


def test_patch_finalizer_failure_becomes_terminal(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def summarize(app_label, status, job_failed_terminal, summary):\n"
        "    empty = {\n"
        '        "finalizer_active": 0,\n'
        "    }\n"
        "    for _item in [None]:\n"
        '        if app_label == "blast":\n'
        "            pass\n"
        '        elif app_label == "finalizer":\n'
        '            summary["finalizer_active"] += status.get("active", 0) or 0\n'
        '    if summary.get("submit_failed_terminal"):\n'
        "        updates = {}\n"
    )

    module._patch_finalizer_failure_status(path)
    first = path.read_text()
    module._patch_finalizer_failure_status(path)

    assert path.read_text() == first
    assert '"finalizer_failed_terminal": 0' in first
    assert 'summary["finalizer_failed_terminal"] += 1' in first
    assert '"phase": "finalizer_failed"' in first
    ast.parse(first)


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


def test_patch_app_recovers_latest_runtime_and_makes_submit_replay_safe(
    tmp_path: Path,
) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def _discover_elb_job_id_from_submit_output(job_id: str, stdout: str) -> str:\n"
        "    if not stdout:\n"
        '        return ""\n'
        "    match = re.search(r\"\\b(?P<elb_job_id>job-[0-9a-fA-F]{32})\\b\", stdout)\n"
        "    if match:\n"
        '        return match.group("elb_job_id").lower()\n'
        '    return ""\n'
        "\n\n"
        "def _effective_elb_job_id(job_info: dict[str, Any]) -> str:\n"
        '    job_id = str(job_info.get("job_id") or "")\n'
        '    current = str(job_info.get("elb_job_id") or "")\n'
        '    canonical_current = re.fullmatch(r"job-[0-9a-f]{32}", current, re.IGNORECASE)\n'
        "    if canonical_current:\n"
        "        return canonical_current.group(0).lower()\n"
        "    discovered = _discover_elb_job_id_from_submit_output(\n"
        "        job_id,\n"
        '        "\\n".join(\n'
        '            str(job_info.get(key) or "")\n'
        '            for key in ("stdout_tail", "stderr_tail")\n'
        "        ),\n"
        "    )\n"
        "    if discovered:\n"
        "        _update_job(job_id, elb_job_id=discovered)\n"
        "        return discovered\n"
        "    return job_id\n"
        "\n\n"
        "def _run_submit_bg(job_id: str) -> None:\n"
        "    with _jobs_lock:\n"
        "        job = dict(_jobs.get(job_id, {}))\n"
        "    if not job:\n"
        "        return\n"
        '    config_text = job.get("config_ini", "")\n'
        "    if not config_text:\n"
        '        _update_job(job_id, status="failed", error="missing persisted config_ini")\n'
        "        return\n"
        "\n"
        "    cfg_path = _write_config_file(job_id, config_text)\n"
        "    cancel_event = _job_cancel_events.setdefault(job_id, Event())\n"
        "    _update_job(\n"
        "        job_id,\n"
        '        status="submitting",\n'
        "        cfg_path=cfg_path,\n"
        '        attempt=int(job.get("attempt", 0)) + 1,\n'
        "        last_progress_at=_now_iso(),\n"
        "    )\n"
        "    result = run_cancellable(\n"
        '                ["elastic-blast", "submit", "--cfg", cfg_path],\n'
        "                timeout=None,\n"
        "                stop_event=cancel_event,\n"
        "            )\n"
        "\n\n"
        "def next_helper() -> None:\n"
        "    pass\n"
    )

    module._harden_openapi_runtime_ids(path)
    module._patch_replay_safe_submit_identity(path)
    first = path.read_text()
    module._patch_replay_safe_submit_identity(path)

    assert path.read_text() == first
    ast.parse(first)
    assert first.index("_save_job(job_id, submit_state, require_persist=True)") < first.index(
        "result = run_cancellable"
    )
    assert '"--json",' in first
    assert '"--idempotency-key",' in first

    request_id = "abcdef123456"
    older_runtime = "job-" + "1" * 32
    newer_runtime = "job-" + "2" * 32
    mismatched_runtime = "job-" + "3" * 32

    def job(
        runtime_id: str,
        *,
        created_at: str,
        resource_version: str,
        path_runtime: str | None = None,
        path_request: str = request_id,
    ) -> dict[str, Any]:
        return {
            "metadata": {
                "labels": {"app": "setup", "elb-job-id": runtime_id},
                "creationTimestamp": created_at,
                "resourceVersion": resource_version,
            },
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "args": [
                                    "-o",
                                    f"https://storage/results/{path_request}/"
                                    f"{path_runtime or runtime_id}",
                                ]
                            }
                        ]
                    }
                }
            },
        }

    payload = {
        "items": [
            job(older_runtime, created_at="2026-09-11T00:41:51Z", resource_version="10"),
            job(newer_runtime, created_at="2026-09-11T01:16:44Z", resource_version="20"),
            job(
                mismatched_runtime,
                created_at="2026-09-11T01:17:00Z",
                resource_version="30",
                path_runtime=newer_runtime,
            ),
            job(
                "job-" + "4" * 32,
                created_at="2026-09-11T01:18:00Z",
                resource_version="40",
                path_request="000000000000",
            ),
        ]
    }
    calls: list[tuple[list[str], int]] = []
    updates: list[tuple[str, str]] = []
    warnings: list[tuple[object, ...]] = []

    def safe_exec(command: list[str], *, timeout: int) -> SimpleNamespace:
        calls.append((command, timeout))
        return SimpleNamespace(stdout=json.dumps(payload))

    def update_job(job_id: str, *, elb_job_id: str) -> None:
        updates.append((job_id, elb_job_id))

    namespace: dict[str, Any] = {
        "Any": Any,
        "hashlib": hashlib,
        "json": json,
        "logger": SimpleNamespace(warning=lambda *args: warnings.append(args)),
        "re": re,
        "safe_exec": safe_exec,
        "_update_job": update_job,
    }
    helper_source = first[: first.index("\n\ndef _run_submit_bg")]
    exec(helper_source, namespace)  # noqa: S102 - generated temporary fixture code.
    effective = namespace["_effective_elb_job_id"]
    deterministic = namespace["_deterministic_elb_job_id"]

    assert (
        effective({"job_id": request_id, "status": "submitting", "attempt": 1})
        == newer_runtime
    )
    assert updates == [(request_id, newer_runtime)]
    assert calls == [(["kubectl", "get", "jobs", "-l", "elb-job-id", "-o", "json"], 15)]
    assert warnings and "duplicate generations" in str(warnings[0][0])
    expected = "job-" + hashlib.sha256(
        f"elb-openapi:{request_id}".encode()
    ).hexdigest()[:32]
    assert deterministic(request_id) == expected
    assert re.fullmatch(r"job-[0-9a-f]{32}", deterministic(request_id))

    calls.clear()
    completed_id = "fedcba654321"
    assert effective({"job_id": completed_id, "status": "completed"}) == completed_id
    assert calls == []

    failed_observation = {
        "job_id": request_id,
        "status": "submitting",
        "attempt": 1,
    }

    def unavailable(_command: list[str], *, timeout: int) -> SimpleNamespace:
        raise TimeoutError(timeout)

    namespace["safe_exec"] = unavailable
    assert effective(failed_observation) == request_id
    assert failed_observation["_runtime_id_observation_failed"] is True
    assert "Kubernetes discovery failed" in str(warnings[-1][0])

    calls.clear()
    fresh = {"job_id": "0123456789ab", "status": "dispatching", "attempt": 0}
    assert effective(fresh) == fresh["job_id"]
    assert "_runtime_id_observation_failed" not in fresh
    assert calls == []

    persisted_runtime = "job-" + "5" * 32
    jobs = {
        request_id: {
            "job_id": request_id,
            "status": "dispatching",
            "config_ini": "[blast]\nprogram = blastn\n",
            "elb_job_id": persisted_runtime,
            "attempt": 0,
        }
    }
    run_calls: list[list[str]] = []
    errors: list[tuple[object, ...]] = []
    run_source = first[
        first.index("def _run_submit_bg") : first.index("\n\ndef next_helper")
    ]
    run_namespace: dict[str, Any] = {
        "Any": Any,
        "Event": lambda: SimpleNamespace(),
        "_effective_elb_job_id": lambda item: str(item["elb_job_id"]),
        "_job_cancel_events": {},
        "_jobs": jobs,
        "_jobs_lock": __import__("contextlib").nullcontext(),
        "_now_iso": lambda: "now",
        "_save_job": lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("persist unavailable")
        ),
        "_update_job": lambda *_args, **_kwargs: None,
        "_write_config_file": lambda *_args: "/tmp/config.ini",  # noqa: S108
        "logger": SimpleNamespace(
            error=lambda *args: errors.append(args),
            warning=lambda *_args: None,
        ),
        "re": re,
        "run_cancellable": lambda command, **_kwargs: run_calls.append(command),
    }
    exec(run_source, run_namespace)  # noqa: S102 - generated temporary fixture code.

    run_namespace["_run_submit_bg"](request_id)

    assert run_calls == []
    assert jobs[request_id]["status"] == "failed"
    assert jobs[request_id]["phase"] == "submit_state_persist_failed"
    assert jobs[request_id]["error"] == "runtime identity could not be persisted"
    assert errors and "persistence failed" in str(errors[0][0])


def test_patch_dead_thread_reclaim_waits_when_k8s_observation_fails(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def reclaim(summary):\n"
        '    if summary.get("total") or summary.get("submit_failed"):\n'
        "        return False\n"
        "    return True\n"
    )

    module._patch_dead_thread_reclaim_observation_guard(path)
    first = path.read_text()
    module._patch_dead_thread_reclaim_observation_guard(path)

    assert path.read_text() == first
    namespace: dict[str, Any] = {}
    exec(first, namespace)  # noqa: S102 - generated temporary fixture code.
    reclaim = namespace["reclaim"]
    assert reclaim({"error": "Kubernetes unavailable"}) is False
    assert reclaim({"total": 1}) is False
    assert reclaim({}) is True


def test_patch_runtime_observation_failure_reaches_reclaim_summary(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        "def refresh(job_id, job):\n"
        "    elb_job_id = _effective_elb_job_id(job)\n"
        '    marker_results_url = str(job.get("results", "")).rstrip("/")\n'
        "    return marker_results_url\n"
    )

    module._patch_runtime_id_observation_fail_closed(path)
    first = path.read_text()
    module._patch_runtime_id_observation_fail_closed(path)

    assert path.read_text() == first
    updates: list[tuple[str, dict[str, Any]]] = []

    def update(job_id: str, **fields: Any) -> dict[str, Any]:
        updates.append((job_id, fields))
        return fields

    namespace: dict[str, Any] = {
        "_effective_elb_job_id": lambda job: (
            job.__setitem__("_runtime_id_observation_failed", True) or "request"
        ),
        "_update_job": update,
    }
    exec(first, namespace)  # noqa: S102 - generated temporary fixture code.

    result = namespace["refresh"]("request-1", {"results": "https://results"})

    assert result == {
        "status": "submitting",
        "phase": "submitting",
        "k8s_summary": {"error": "runtime identity observation unavailable"},
    }
    assert updates == [
        (
            "request-1",
            {
                "status": "submitting",
                "phase": "submitting",
                "k8s_summary": {"error": "runtime identity observation unavailable"},
            },
        )
    ]


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


def test_replace_fresh_or_legacy_accepts_nested_legacy_form(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    fresh = "base\n"
    legacy = fresh + "legacy hardening\n"
    path.write_text(f"before\n{legacy}after\n")

    module._replace_fresh_or_legacy(
        path,
        fresh=fresh,
        legacy=legacy,
        desired="desired\n",
        marker="desired",
    )
    first = path.read_text()
    module._replace_fresh_or_legacy(
        path,
        fresh=fresh,
        legacy=legacy,
        desired="desired\n",
        marker="desired",
    )

    assert first == "before\ndesired\nafter\n"
    assert path.read_text() == first


def test_replace_fresh_or_legacy_rejects_independent_hybrid_state(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    original = "before\nfresh form\nmiddle\nlegacy form\nafter\n"
    path.write_text(original)

    with pytest.raises(RuntimeError, match="ambiguous fresh/legacy"):
        module._replace_fresh_or_legacy(
            path,
            fresh="fresh form\n",
            legacy="legacy form\n",
            desired="desired\n",
            marker="desired",
        )

    assert path.read_text() == original


def test_replace_fresh_or_legacy_rejects_unknown_state(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text("upstream changed\n")

    with pytest.raises(RuntimeError, match="found 0"):
        module._replace_fresh_or_legacy(
            path,
            fresh="fresh form\n",
            legacy="legacy form\n",
            desired="desired\n",
            marker="desired",
        )

    assert path.read_text() == "upstream changed\n"


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
