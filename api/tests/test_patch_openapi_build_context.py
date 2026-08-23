"""Tests for the OpenAPI image build-context patcher.

Responsibility: Verify that OpenAPI Dockerfile patching fails the image build when
ElasticBLAST runtime TTL policy is missing.
Edit boundaries: Use temporary build contexts only; never invoke Docker or Azure.
Key entry points: `test_patch_dockerfile_asserts_ttl_in_all_runtime_copies`.
Risky contracts: The assertions must cover source, system Python, and venv templates.
Validation: `uv run pytest -q api/tests/test_patch_openapi_build_context.py`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


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
    assert text.count("grep -q 'ttlSecondsAfterFinished:'") == 3
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
    assert text.count("python3 /tmp/patch_elastic_blast.py") == 1
    assert text.count("cp -a /tmp/elb-src/src/elastic_blast/templates/.") == 2
    assert text.count("grep -q 'ttlSecondsAfterFinished:'") == 3
