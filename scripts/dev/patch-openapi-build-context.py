#!/usr/bin/env python3
# ruff: noqa: E501
"""Patch the sibling docker-openapi build context for dashboard runtime policy.

Responsibility: Patch the sibling docker-openapi build context for dashboard runtime policy
Edit boundaries: Keep this as an operator/dev utility; do not make production code depend on it.
Key entry points: `_replace_once`, `_insert_once`, `_copy_support_files`, `patch_dockerfile`,
`_disable_warmed_cache_skip`, `_harden_openapi_runtime_ids`, `patch_app`, `main`
Risky contracts: Assume local developer context only; avoid broad production-side effects.
Validation: `uv run pytest -q api/tests/test_patch_openapi_build_context.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _force_elb_ref(path: Path, ref: str) -> None:
    """Pin ``ARG ELB_REF=<ref>`` regardless of the current value.

    The sibling Dockerfile's ``ARG ELB_REF`` default drifts with upstream WIP,
    so an exact-string replace breaks every time it advances. Match the line by
    shape and rewrite it to the dashboard's known-good ref (idempotent).
    """
    import re

    text = path.read_text()
    pattern = re.compile(r"^ARG ELB_REF=.*$", re.MULTILINE)
    count = len(pattern.findall(text))
    if count != 1:
        raise RuntimeError(f"expected one ARG ELB_REF line in {path}, found {count}")
    path.write_text(pattern.sub(f"ARG ELB_REF={ref}", text, count=1))


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if new and new in text:
        # Idempotent re-run: the final form of this replacement is already
        # present in the file. This tolerates the sibling Dockerfile / app
        # catching up to upstream (e.g. ``ARG ELB_REF`` advancing past the
        # value we used to inject, OR the venv-stage block being added
        # natively upstream so the dashboard insertion would otherwise
        # duplicate it).
        return
    count = text.count(old)
    if not new and count == 0:
        return
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def _replace_once_unless_marker(
    path: Path,
    old: str,
    new: str,
    marker: str,
) -> None:
    text = path.read_text()
    if marker in text:
        return
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, found {count}")
    path.write_text(text.replace(old, new, 1))


def _insert_once(path: Path, anchor: str, insertion: str, marker: str) -> None:
    text = path.read_text()
    if marker in text:
        return
    count = text.count(anchor)
    if count != 1:
        raise RuntimeError(f"expected one anchor in {path}, found {count}")
    path.write_text(text.replace(anchor, anchor + insertion, 1))


def _replace_fresh_or_legacy(
    path: Path,
    *,
    fresh: str,
    legacy: str,
    desired: str,
    marker: str,
) -> None:
    """Apply ``desired`` to either an unpatched or previously patched context."""
    text = path.read_text()
    if marker in text:
        return
    source = legacy if legacy in text else fresh
    count = text.count(source)
    if count != 1:
        raise RuntimeError(f"expected one fresh/legacy match in {path}, found {count}")
    path.write_text(text.replace(source, desired, 1))


def _copy_support_files(root: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    for name in ("patch_elastic_blast.py", "merge-sharded-results.sh"):
        src = project_root / "terminal" / name
        dest = root / name
        if not src.is_file():
            raise RuntimeError(f"missing OpenAPI build support file: {src}")
        if not dest.exists() or dest.read_bytes() != src.read_bytes():
            dest.write_bytes(src.read_bytes())


def _copy_app_overlay(root: Path) -> None:
    """Copy the self-learning ETA overlay into the build-context ``app/``.

    The Dockerfile already ``COPY ./app /app`` so dropping ``eta.py`` next to
    ``main.py`` is enough to make ``import eta`` resolve at runtime. The overlay
    is import-safe and strictly opt-in (``ELB_OPENAPI_ETA_ENABLED``).
    """
    project_root = Path(__file__).resolve().parents[2]
    src = project_root / "scripts" / "dev" / "openapi-overlays" / "eta.py"
    if not src.is_file():
        raise RuntimeError(f"missing OpenAPI ETA overlay: {src}")
    dest = root / "app" / "eta.py"
    if not dest.exists() or dest.read_bytes() != src.read_bytes():
        dest.write_bytes(src.read_bytes())


def _patch_terminal_webhook_runtime_id(path: Path) -> None:
    """Attach a genuine ElasticBLAST runtime id to terminal webhooks."""

    _insert_once(
        path,
        "            merged = {**job_snap, **updates}\n",
        (
            "            runtime_job_id = _effective_elb_job_id(merged)\n"
            "            if runtime_job_id.startswith(\"job-\") and runtime_job_id != job_id:\n"
            "                payload[\"elb_job_id\"] = runtime_job_id\n"
        ),
        'payload["elb_job_id"] = runtime_job_id',
    )


def _disable_warmed_cache_skip(path: Path) -> None:
    """Remove the unsafe node-local cache skip hint from generated configs."""

    marker = "Completed warmup Jobs are not node-local cache-presence proofs."
    _insert_once(
        path,
        '    config["blast"]["db"] = db_url\n',
        (
            "    # Completed warmup Jobs are not node-local cache-presence proofs.\n"
            "    # Always let the hardened init path validate and repair every shard.\n"
            '    config["cluster"].pop("exp-skip-warmed-ssd-init", None)\n'
        ),
        marker,
    )
    text = path.read_text()
    assignment = 'config["cluster"]["exp-skip-warmed-ssd-init"] = "true"'
    removal = 'config["cluster"].pop("exp-skip-warmed-ssd-init", None)'
    if removal not in text:
        raise RuntimeError("warmed-cache safety removal is missing after patching")
    if text.rfind(assignment) > text.rfind(removal):
        raise RuntimeError("warmed-cache skip assignment appears after the safety removal")


def _harden_openapi_runtime_ids(path: Path) -> None:
    """Restrict OpenAPI runtime correlation to canonical ElasticBLAST IDs."""

    text = path.read_text()
    if text.count("def _discover_elb_job_id_from_submit_output(") != 1:
        raise RuntimeError("expected exactly one OpenAPI runtime-id discovery helper")
    if text.count("def _effective_elb_job_id(") != 1:
        raise RuntimeError("expected exactly one OpenAPI effective runtime-id helper")
    start = text.find("def _discover_elb_job_id_from_submit_output(")
    if start < 0:
        raise RuntimeError("missing OpenAPI runtime-id discovery helper")
    end = text.find("\n\ndef ", start + 1)
    if end < 0:
        raise RuntimeError("could not isolate OpenAPI runtime-id discovery helper")
    effective_start = text.find("def _effective_elb_job_id(", end)
    if effective_start < 0:
        raise RuntimeError("missing OpenAPI effective runtime-id helper")
    effective_end = text.find("\n\ndef ", effective_start + 1)
    if effective_end < 0:
        raise RuntimeError("could not isolate OpenAPI effective runtime-id helper")

    block = text[start:effective_end]
    block = block.replace(
        "(?P<elb_job_id>job-[A-Za-z0-9_-]+)",
        "(?P<elb_job_id>job-[0-9a-fA-F]{32})",
    )
    block = block.replace(
        'r"\\b(?P<elb_job_id>job-[0-9a-f]{32})\\b"',
        'r"\\b(?P<elb_job_id>job-[0-9a-fA-F]{32})\\b"',
    )
    block = block.replace(
        '            return match.group("elb_job_id")\n',
        '            return match.group("elb_job_id").lower()\n',
    )
    block = block.replace(
        '    if current.startswith("job-"):\n'
        "        return current\n",
        '    canonical_current = re.fullmatch(r"job-[0-9a-f]{32}", current, re.IGNORECASE)\n'
        "    if canonical_current:\n"
        "        return canonical_current.group(0).lower()\n",
    )
    block = block.replace("    return current or job_id\n", "    return job_id\n")

    unsafe_fragments = (
        "job-[A-Za-z0-9_-]+",
        'current.startswith("job-")',
        "return current or job_id",
    )
    if any(fragment in block for fragment in unsafe_fragments):
        raise RuntimeError("OpenAPI runtime-id helper remains permissive after patching")
    required_fragments = (
        "job-[0-9a-fA-F]{32}",
        "canonical_current = re.fullmatch",
        'return match.group("elb_job_id").lower()',
        "return canonical_current.group(0).lower()",
        "return job_id",
    )
    if any(fragment not in block for fragment in required_fragments):
        raise RuntimeError("OpenAPI runtime-id helper does not satisfy the canonical contract")
    path.write_text(text[:start] + block + text[effective_end:])


def _harden_openapi_runtime_id_consumers(path: Path) -> None:
    """Require canonical IDs at every injected OpenAPI correlation boundary."""

    text = path.read_text()
    replacements = (
        (
            'if runtime_job_id.startswith("job-") and runtime_job_id != job_id:',
            'if re.fullmatch(r"job-[0-9a-f]{32}", runtime_job_id, re.IGNORECASE) '
            "and runtime_job_id != job_id:",
        ),
        (
            'if elb_job_id.startswith("job-"):',
            'if re.fullmatch(r"job-[0-9a-f]{32}", elb_job_id, re.IGNORECASE):',
        ),
        (
            'if effective_elb_job_id.startswith("job-") and '
            "job_info.get(\"elb_job_id\") != effective_elb_job_id:",
            'if re.fullmatch(r"job-[0-9a-f]{32}", effective_elb_job_id, re.IGNORECASE) '
            'and job_info.get("elb_job_id") != effective_elb_job_id:',
        ),
        (
            'if effective_elb_job_id.startswith("job-") and effective_elb_job_id != str(',
            'if re.fullmatch(r"job-[0-9a-f]{32}", effective_elb_job_id, re.IGNORECASE) '
            "and effective_elb_job_id != str(",
        ),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    unsafe = (
        'runtime_job_id.startswith("job-")',
        'elb_job_id.startswith("job-")',
        'effective_elb_job_id.startswith("job-")',
    )
    if any(fragment in text for fragment in unsafe):
        raise RuntimeError("OpenAPI runtime-id consumer remains permissive after patching")
    path.write_text(text)


def _validate_dockerfile_runtime_policy(path: Path) -> None:
    """Verify safety semantics independently from idempotency markers."""

    text = path.read_text()
    expected_counts = {
        "ARG ELB_REF=744d79b": 1,
        "COPY patch_elastic_blast.py /tmp/patch_elastic_blast.py": 1,
        "COPY merge-sharded-results.sh /tmp/merge-sharded-results.sh": 1,
        "python3 /tmp/patch_elastic_blast.py /tmp/elb-src": 1,
        "grep -q 'ttlSecondsAfterFinished:'": 3,
        "for template in job-init-ssd-shard-aks.yaml.template": 3,
        'elb-job-id: \"${BLAST_ELB_JOB_ID}\"': 3,
        "|| exit 1; done": 3,
        "cp -a /tmp/elb-src/src/elastic_blast/templates/.": 2,
    }
    mismatches = {
        fragment: (text.count(fragment), expected)
        for fragment, expected in expected_counts.items()
        if text.count(fragment) != expected
    }
    if mismatches:
        raise RuntimeError(f"OpenAPI Dockerfile runtime policy mismatch: {mismatches}")


def _validate_openapi_runtime_policy(path: Path) -> None:
    """Verify generated app semantics independently from patch markers."""

    text = path.read_text()
    required = (
        'config["cluster"].pop("exp-skip-warmed-ssd-init", None)',
        "def _discover_elb_job_id_from_submit_output(",
        "def _effective_elb_job_id(",
        'canonical_current = re.fullmatch(r"job-[0-9a-f]{32}"',
        'payload["elb_job_id"] = runtime_job_id',
        'def _job_marker_phase(results_url: str, elb_job_id: str = "")',
        're.fullmatch(r"job-[0-9a-f]{32}", elb_job_id, re.IGNORECASE)',
        'safe_exec(["kubectl", "get", "jobs", "-l", f"elb-job-id={elb_job_id}"',
        'safe_exec(["kubectl", "get", "pods", "-l", f"elb-job-id={elb_job_id}"',
    )
    missing = [fragment for fragment in required if fragment not in text]
    forbidden = (
        'runtime_job_id.startswith("job-")',
        'elb_job_id.startswith("job-")',
        'effective_elb_job_id.startswith("job-")',
        'safe_exec(["kubectl", "get", "jobs", "-o", "json"]',
        'safe_exec(["kubectl", "get", "pods", "-o", "json"]',
    )
    present = [fragment for fragment in forbidden if fragment in text]
    assignment = 'config["cluster"]["exp-skip-warmed-ssd-init"] = "true"'
    removal = 'config["cluster"].pop("exp-skip-warmed-ssd-init", None)'
    if missing or present or text.rfind(assignment) > text.rfind(removal):
        raise RuntimeError(
            "OpenAPI app runtime policy mismatch: "
            f"missing={missing}, forbidden={present}"
        )


def patch_dockerfile(root: Path) -> None:
    _copy_support_files(root)
    path = root / "Dockerfile"
    # Force the elastic-blast source ref the OpenAPI image installs to a
    # known-good commit. ``744d79b`` is the one-commit successor to the previous
    # ``7a471297`` pin: it retains ``bin/elastic-blast`` and taxonomy staging,
    # and adds the guarded ``exp-skip-warmed-ssd-init`` config/runtime support.
    # The sibling
    # Dockerfile's ``ARG ELB_REF`` default drifts with upstream WIP (e.g.
    # ``5b7ea2b`` dropped ``bin/elastic-blast`` and breaks the build), so pin it
    # here regardless of the current value rather than matching one exact string.
    _force_elb_ref(path, "744d79b")
    _insert_once(
        path,
        "COPY ./app /app\n",
        (
            "COPY patch_elastic_blast.py /tmp/patch_elastic_blast.py\n"
            "COPY merge-sharded-results.sh /tmp/merge-sharded-results.sh\n"
        ),
        "COPY patch_elastic_blast.py /tmp/patch_elastic_blast.py",
    )
    checkout_anchor = "    git -C /tmp/elb-src checkout ${ELB_REF} && \\\n"
    legacy_patch = (
        "    python3 /tmp/patch_elastic_blast.py /tmp/elb-src "
        "/tmp/merge-sharded-results.sh && \\\n"
    )
    source_ttl_check = (
        "    grep -q 'ttlSecondsAfterFinished:' "
        "/tmp/elb-src/src/elastic_blast/templates/"
        "blast-batch-job-aks.yaml.template && \\\n"
    )
    _replace_fresh_or_legacy(
        path,
        fresh=checkout_anchor,
        legacy=checkout_anchor + legacy_patch,
        desired=checkout_anchor + legacy_patch + source_ttl_check,
        marker=source_ttl_check.strip(),
    )
    identity_templates = (
        "job-init-ssd-shard-aks.yaml.template "
        "blast-batch-job-shard-ssd-aks.yaml.template "
        "elb-finalizer-aks.yaml.template"
    )
    source_identity_check = (
        f"    for template in {identity_templates}; do "
        "grep -q 'elb-job-id: \"${BLAST_ELB_JOB_ID}\"' "
        '"/tmp/elb-src/src/elastic_blast/templates/${template}" || exit 1; done && \\\n'
    )
    _insert_once(
        path,
        source_ttl_check,
        source_identity_check,
        source_identity_check.strip(),
    )
    _replace_once(
        path,
        "    rm -rf /tmp/elb-src && \\\n",
        "    true && \\\n",
    )
    system_install = "    pip3 install --no-cache-dir --no-build-isolation /tmp/elb-src && \\\n"
    system_copy = (
        "    cp -a /tmp/elb-src/src/elastic_blast/templates/. "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/ && \\\n"
    )
    system_check = (
        "    grep -q 'ttlSecondsAfterFinished:' "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/"
        "blast-batch-job-aks.yaml.template && \\\n"
    )
    _replace_fresh_or_legacy(
        path,
        fresh=system_install,
        legacy=system_install + system_copy,
        desired=system_install + system_copy + system_check,
        marker=system_check.strip(),
    )
    system_identity_check = (
        f"    for template in {identity_templates}; do "
        "grep -q 'elb-job-id: \"${BLAST_ELB_JOB_ID}\"' "
        '"/usr/local/lib/python3.11/site-packages/elastic_blast/templates/${template}" '
        "|| exit 1; done && \\\n"
    )
    _insert_once(
        path,
        system_check,
        system_identity_check,
        system_identity_check.strip(),
    )
    venv_install = "    && pip install --no-cache-dir azure-cli \\\n"
    venv_legacy = (
        venv_install
        + "    && pip install --no-cache-dir --no-deps --no-build-isolation /tmp/elb-src \\\n"
        + "    && cp -a /tmp/elb-src/src/elastic_blast/templates/. /opt/venv/lib/python3.11/site-packages/elastic_blast/templates/ \\\n"
        + "    && rm -rf /tmp/elb-src \\\n"
    )
    venv_check = (
        "    && grep -q 'ttlSecondsAfterFinished:' "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/"
        "blast-batch-job-aks.yaml.template \\\n"
    )
    venv_desired = (
        venv_install
        + "    && pip install --no-cache-dir --no-deps --no-build-isolation /tmp/elb-src \\\n"
        + "    && cp -a /tmp/elb-src/src/elastic_blast/templates/. /opt/venv/lib/python3.11/site-packages/elastic_blast/templates/ \\\n"
        + venv_check
        + "    && rm -rf /tmp/elb-src \\\n"
    )
    _replace_fresh_or_legacy(
        path,
        fresh=venv_install,
        legacy=venv_legacy,
        desired=venv_desired,
        marker=venv_check.strip(),
    )
    venv_identity_check = (
        f"    && for template in {identity_templates}; do "
        "grep -q 'elb-job-id: \"${BLAST_ELB_JOB_ID}\"' "
        '"/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/${template}" '
        "|| exit 1; done \\\n"
    )
    _insert_once(
        path,
        venv_check,
        venv_identity_check,
        venv_identity_check.strip(),
    )
    _validate_dockerfile_runtime_policy(path)


def patch_app(root: Path) -> None:
    _copy_app_overlay(root)
    path = root / "app" / "main.py"
    _patch_terminal_webhook_runtime_id(path)
    _disable_warmed_cache_skip(path)
    if "def _effective_elb_job_id(" not in path.read_text():
        _replace_once(
            path,
            "    return None\n\n\ndef _ensure_elb_scripts_configmap() -> None:\n",
            (
            "    return None\n\n\n"
            "def _discover_elb_job_id_from_submit_output(job_id: str, stdout: str) -> str:\n"
            "    if not stdout:\n"
            '        return ""\n'
            "    patterns = (\n"
            '        rf"/results/(?:\\d{{4}}/\\d{{2}}/\\d{{2}}/)?{re.escape(job_id)}/(?P<elb_job_id>job-[0-9a-fA-F]{{32}})/metadata/",\n'
            '        r"\\b(?P<elb_job_id>job-[0-9a-fA-F]{32})\\b",\n'
            "    )\n"
            "    for pattern in patterns:\n"
            "        match = re.search(pattern, stdout)\n"
            "        if match:\n"
            '            return match.group("elb_job_id").lower()\n'
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
                "def _ensure_elb_scripts_configmap() -> None:\n"
            ),
        )
    _harden_openapi_runtime_ids(path)
    _insert_once(
        path,
        (
            '    config["cluster"]["num-nodes"] = str(NUM_NODES)\n'
            '    config["blast"]["program"] = req.program\n'
        ),
        (
            "    # Dashboard policy: OpenAPI submissions use AKS node-local SSD,\n"
            "    # not the historical shared PV/PVC path.\n"
            '    config["cluster"]["exp-use-local-ssd"] = "true"\n'
            '    config["cluster"]["reuse"] = "true"\n'
        ),
        "Dashboard policy: OpenAPI submissions use AKS node-local SSD",
    )
    _insert_once(
        path,
        '    if req.batch_len is not None:\n        config["blast"]["batch-len"] = str(req.batch_len)\n',
        (
            "\n    db_name = _db_name_from_value(req.db)\n"
            '    profile = str(req.resource_profile or "").strip().lower()\n'
            '    if db_name == "core_nt" and profile in {"core_nt_precise", "precise", "core_nt_safe"}:\n'
            "        partitions = max(1, min(NUM_NODES, 10))\n"
            '        config["blast"]["db-partitions"] = str(partitions)\n'
            '        config["blast"]["db-partition-prefix"] = (\n'
            '            f"{_blob_base()}/blast-db/{partitions}shards/core_nt_shard_"\n'
            "        )\n"
            '        if "-searchsp" not in opts and "-dbsize" not in opts:\n'
            '            config["blast"]["options"] = f"{opts} -searchsp 32156241807668"\n'
        ),
        'profile in {"core_nt_precise", "precise", "core_nt_safe"}',
    )
    _insert_once(
        path,
        '    if req.batch_len is not None:\n        config["blast"]["batch-len"] = str(req.batch_len)\n',
        (
            "\n    # Dashboard concurrency lever (default-OFF): ELB_OPENAPI_NUM_CPUS pins the\n"
            "    # elastic-blast [cluster] num-cpus. elastic-blast derives the shard pod CPU\n"
            "    # limit (= num-cpus) and request (= num-cpus - 2) from it, so lowering this\n"
            "    # raises how many shard pods co-schedule per node (request is the binding\n"
            "    # constraint). Unset => elastic-blast keeps its profile default\n"
            "    # (threads_per_pod, currently 8 -> request 6 -> 2 jobs/node), i.e. unchanged\n"
            "    # behaviour. Search space / sharding / num-nodes are untouched, so NCBI\n"
            "    # parity (-searchsp) is independent of this knob.\n"
            '    _elb_num_cpus = os.environ.get("ELB_OPENAPI_NUM_CPUS", "").strip()\n'
            "    if _elb_num_cpus:\n"
            "        try:\n"
            "            _elb_num_cpus_val = int(_elb_num_cpus)\n"
            "        except ValueError:\n"
            "            _elb_num_cpus_val = 0\n"
            "        if _elb_num_cpus_val >= 1:\n"
            '            config["cluster"]["num-cpus"] = str(_elb_num_cpus_val)\n'
        ),
        "ELB_OPENAPI_NUM_CPUS",
    )
    text = path.read_text()
    duplicate = (
        "    db_name = _db_name_from_value(req.db)\n    blast_version = _blast_version_detail()"
    )
    if duplicate in text:
        path.write_text(text.replace(duplicate, "    blast_version = _blast_version_detail()", 1))
    _replace_once(
        path,
        "        _update_job(\n"
        "            job_id,\n"
        "            status=status,\n"
        '            phase="submitted" if status == "running" else status,\n'
        '            elb_job_id=payload.get("correlation_id") or job_id,\n',
        "        _update_job(\n"
        "            job_id,\n"
        "            status=status,\n"
        '            phase="submitted" if status == "running" else status,\n'
        "            elb_job_id=(\n"
        '                payload.get("correlation_id")\n'
        '                or _discover_elb_job_id_from_submit_output(job_id, result.stdout or "")\n'
        "                or job_id\n"
        "            ),\n",
    )
    _replace_once_unless_marker(
        path,
        "def _job_marker_phase(results_url: str) -> str | None:\n"
        "    if not results_url:\n"
        "        return None\n"
        "    try:\n"
        "        _azcopy_login()\n"
        '        proc = safe_exec(["azcopy", "ls", f"{results_url}/metadata/"], timeout=10)\n'
        "    except Exception:\n"
        "        return None\n"
        '    if "SUCCESS.txt" in proc.stdout:\n'
        '        return "completed"\n'
        '    if "FAILURE.txt" in proc.stdout:\n'
        '        return "failed"\n'
        "    return None\n",
        'def _job_marker_phase(results_url: str, elb_job_id: str = "") -> str | None:\n'
        "    if not results_url:\n"
        "        return None\n"
        '    base = results_url.rstrip("/")\n'
        '    candidates = [f"{base}/metadata/"]\n'
        '    if elb_job_id.startswith("job-"):\n'
        '        candidates.insert(0, f"{base}/{elb_job_id}/metadata/")\n'
        "    for marker_url in candidates:\n"
        "        try:\n"
        "            _azcopy_login()\n"
        '            proc = safe_exec(["azcopy", "ls", marker_url], timeout=10)\n'
        "        except Exception:\n"
        "            continue\n"
        '        if "SUCCESS.txt" in proc.stdout:\n'
        '            return "completed"\n'
        '        if "FAILURE.txt" in proc.stdout:\n'
        '            return "failed"\n'
        "    return None\n",
        'def _job_marker_phase(results_url: str, elb_job_id: str = "")',
    )
    _replace_once(
        path,
        "    if not items:\n"
        "        try:\n"
        '            proc = safe_exec(["kubectl", "get", "jobs", "-o", "json"], timeout=15)\n'
        "            fallback = json.loads(proc.stdout)\n"
        "            items = [\n"
        "                item\n"
        '                for item in fallback.get("items", [])\n'
        '                if item.get("metadata", {}).get("labels", {}).get("app") in {"blast", "submit", "finalizer"}\n'
        "            ]\n"
        "        except Exception:\n"
        "            items = []\n"
        "\n",
        "",
    )
    _replace_once(
        path,
        "    if not items:\n"
        "        try:\n"
        '            proc = safe_exec(["kubectl", "get", "pods", "-o", "json"], timeout=15)\n'
        "            fallback = json.loads(proc.stdout)\n"
        "            items = [\n"
        "                item\n"
        '                for item in fallback.get("items", [])\n'
        '                if item.get("metadata", {}).get("labels", {}).get("app") in {"blast", "submit", "finalizer"}\n'
        "            ]\n"
        "        except Exception:\n"
        "            items = []\n"
        "\n",
        "",
    )
    _replace_once(
        path,
        '    marker = _job_marker_phase(job.get("results", ""))\n',
        "    elb_job_id = _effective_elb_job_id(job)\n"
        '    marker = _job_marker_phase(job.get("results", ""), elb_job_id)\n',
    )
    _replace_once(
        path,
        '    elb_job_id = job.get("elb_job_id") or job_id\n',
        "    elb_job_id = _effective_elb_job_id(job)\n",
    )
    _insert_once(
        path,
        '    }\n    summary = job_info.get("k8s_summary") if isinstance(job_info.get("k8s_summary"), dict) else {}\n',
        (
            "    effective_elb_job_id = _effective_elb_job_id(job_info)\n"
            '    if effective_elb_job_id.startswith("job-") and job_info.get("elb_job_id") != effective_elb_job_id:\n'
            "        fresh_summary = _k8s_job_summary(effective_elb_job_id)\n"
            "        updated = _update_job(\n"
            '            job_info["job_id"],\n'
            "            elb_job_id=effective_elb_job_id,\n"
            "            k8s_summary=fresh_summary,\n"
            "            last_progress_at=_now_iso(),\n"
            "        )\n"
            "        if updated:\n"
            "            job_info = updated\n"
            "        summary = fresh_summary\n"
        ),
        "effective_elb_job_id = _effective_elb_job_id(job_info)",
    )

    # ── Self-learning ETA (default-OFF via ELB_OPENAPI_ETA_ENABLED) ──────────
    # The overlay module (app/eta.py) learns per-(db, query-size, cold/warm) run
    # times online and simulates the MAX_ACTIVE_SUBMISSIONS-server queue to
    # project per-job start/finish. Every hook is gated on _eta.enabled() so the
    # unset default is byte-identical to legacy (no extra job-state writes).
    _insert_once(
        path,
        "from util import run_cancellable, safe_exec\n",
        (
            "\ntry:\n"
            "    import eta as _eta\n"
            "except Exception:  # pragma: no cover - ETA overlay is optional\n"
            "    _eta = None\n"
        ),
        "import eta as _eta",
    )
    _insert_once(
        path,
        '        "job_id": job_id, "status": "queued", "mode": "B" if is_b else "A",\n',
        (
            '        "query_seqs": (_eta.parse_query_features(req.query_fasta)[0] if (_eta is not None and _eta.enabled() and is_b) else 0),\n'
            '        "query_bases": (_eta.parse_query_features(req.query_fasta)[1] if (_eta is not None and _eta.enabled() and is_b) else 0),\n'
        ),
        '"query_seqs":',
    )
    # Completion-sample recording is hooked into the single state-write choke
    # point _update_job (NOT a status-payload builder) so learning happens on
    # the terminal transition regardless of which endpoint — or the background
    # watchdog — observes it. The atomic `eta_recorded` flag (claimed under
    # _jobs_lock, persisted via _save_job_cm) guarantees exactly-once recording
    # even under concurrent writes.
    _replace_once(
        path,
        "        data = dict(current)\n"
        "        data.update(updates)\n"
        '        data["updated_at"] = _now_iso()\n'
        "        _jobs[job_id] = data\n"
        "    _save_job_cm(job_id, data)\n"
        "    return data\n",
        "        data = dict(current)\n"
        "        data.update(updates)\n"
        '        data["updated_at"] = _now_iso()\n'
        "        _eta_snapshot = None\n"
        "        if (\n"
        "            _eta is not None\n"
        "            and _eta.enabled()\n"
        '            and updates.get("status") == "completed"\n'
        '            and not current.get("eta_recorded")\n'
        "        ):\n"
        '            data["eta_recorded"] = True\n'
        "            _jobs[job_id] = data\n"
        "            _eta_snapshot = [dict(v) for v in _jobs.values()]\n"
        "        else:\n"
        "            _jobs[job_id] = data\n"
        "    _save_job_cm(job_id, data)\n"
        "    if _eta_snapshot is not None:\n"
        "        try:\n"
        "            _eta.record_sample(data, _eta_snapshot)\n"
        "        except Exception:\n"
        "            pass\n"
        "    return data\n",
    )
    _replace_once(
        path,
        '    if public_status == "queued":\n'
        '        payload["queue_position"] = _queued_position(job_info["job_id"])\n'
        '    elif public_status == "running":\n'
        '        payload["progress_pct"] = _progress_pct(job_info)\n',
        '    if public_status == "queued":\n'
        '        payload["queue_position"] = _queued_position(job_info["job_id"])\n'
        "        if _eta is not None and _eta.enabled():\n"
        "            with _jobs_lock:\n"
        "                _eta_jobs = [dict(v) for v in _jobs.values()]\n"
        "            _eta_out = _eta.compute_eta(job_info, _eta_jobs, MAX_ACTIVE_SUBMISSIONS)\n"
        "            if _eta_out:\n"
        '                payload["eta"] = _eta_out\n'
        '    elif public_status == "running":\n'
        '        payload["progress_pct"] = _progress_pct(job_info)\n'
        "        if _eta is not None and _eta.enabled():\n"
        "            with _jobs_lock:\n"
        "                _eta_jobs = [dict(v) for v in _jobs.values()]\n"
        "            _eta_out = _eta.compute_eta(job_info, _eta_jobs, MAX_ACTIVE_SUBMISSIONS)\n"
        "            if _eta_out:\n"
        '                payload["eta"] = _eta_out\n',
    )    # Primary polling endpoint GET /v1/jobs/{id}/status (get_job_status) builds
    # its own inline dict and does NOT route through _external_job_payload, so
    # the ETA hook above never reaches it. Inject the same gated projection here
    # so callers polling the canonical status_url see `eta` for active/queued
    # jobs. Terminal jobs are skipped (compute_eta returns None anyway).
    _replace_once(
        path,
        '    return {\n'
        '        "job_id": job_id,\n'
        '        "status": job_info.get("status", "unknown"),\n',
        '    _status_payload: dict[str, Any] = {\n'
        '        "job_id": job_id,\n'
        '        "status": job_info.get("status", "unknown"),\n',
    )
    _replace_once(
        path,
        '        "kubernetes": {"summary": job_info.get("k8s_summary", {})},\n'
        "    }\n",
        '        "kubernetes": {"summary": job_info.get("k8s_summary", {})},\n'
        "    }\n"
        "    if _eta is not None and _eta.enabled() and job_info.get(\"status\") in {\"queued\", \"dispatching\", \"submitting\", \"running\"}:\n"
        "        with _jobs_lock:\n"
        "            _eta_jobs = [dict(v) for v in _jobs.values()]\n"
        "        _eta_out = _eta.compute_eta(job_info, _eta_jobs, MAX_ACTIVE_SUBMISSIONS)\n"
        "        if _eta_out:\n"
        '            _status_payload["eta"] = _eta_out\n'
        "    return _status_payload\n",
    )
    _harden_openapi_runtime_id_consumers(path)
    _validate_openapi_runtime_policy(path)

def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch-openapi-build-context.py /path/to/docker-openapi", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    if not (root / "Dockerfile").is_file() or not (root / "app" / "main.py").is_file():
        print(f"not a docker-openapi build context: {root}", file=sys.stderr)
        return 2
    patch_dockerfile(root)
    patch_app(root)
    print("patched docker-openapi build context for dashboard OpenAPI runtime policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
