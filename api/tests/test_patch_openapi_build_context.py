"""Tests for the OpenAPI image build-context patcher.

Responsibility: Verify OpenAPI image patching enforces runtime policy and refreshes stale
ElasticBLAST scripts.
Edit boundaries: Use temporary build contexts only; never invoke Docker or Azure.
Key entry points: `test_patch_dockerfile_asserts_ttl_in_all_runtime_copies`,
`test_patch_app_reconciles_elb_scripts_by_content`.
Risky contracts: The assertions must cover source, system Python, and venv templates; OpenAPI
submits must never trust historical warmup Jobs or name-only ConfigMap checks as node-local
cache-presence proof.
Validation: `uv run pytest -q api/tests/test_patch_openapi_build_context.py`.
"""

from __future__ import annotations

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
