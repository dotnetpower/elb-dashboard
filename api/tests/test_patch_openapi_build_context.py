"""Tests for the OpenAPI image build-context patcher.

Responsibility: Verify that OpenAPI Dockerfile patching fails the image build when
ElasticBLAST runtime TTL policy is missing.
Edit boundaries: Use temporary build contexts only; never invoke Docker or Azure.
Key entry points: `test_patch_dockerfile_asserts_ttl_in_all_runtime_copies`.
Risky contracts: The assertions must cover source, system Python, and venv templates; OpenAPI
submits must never trust historical warmup Jobs as node-local cache-presence proof.
Validation: `uv run pytest -q api/tests/test_patch_openapi_build_context.py`.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts/dev/patch-openapi-build-context.py"
    spec = importlib.util.spec_from_file_location("patch_openapi_build_context", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    import pytest

    with pytest.raises(RuntimeError, match="assignment appears after"):
        module._disable_warmed_cache_skip(path)


def test_patch_app_rejects_marker_without_warmed_cache_removal(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "main.py"
    path.write_text(
        '    config["blast"]["db"] = db_url\n'
        "    # Completed warmup Jobs are not node-local cache-presence proofs.\n"
    )

    import pytest

    with pytest.raises(RuntimeError, match="safety removal is missing"):
        module._disable_warmed_cache_skip(path)


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

    import pytest

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
