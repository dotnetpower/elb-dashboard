#!/usr/bin/env python3
# ruff: noqa: E501
"""Patch the sibling docker-openapi build context for dashboard runtime policy.

The sibling repository remains the source for the OpenAPI service, but this
dashboard currently needs two runtime guarantees that are not safe to leave to
the historical image defaults:

* the uvicorn Python environment must see the installed ``elastic_blast``
  package so it can create the ``elb-scripts`` ConfigMap;
* ``resource_profile=core_nt_precise`` must emit the local-SSD, 10-shard
  ``core_nt`` ElasticBLAST config used by the dashboard validation path.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
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


def _copy_support_files(root: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    for name in ("patch_elastic_blast.py", "merge-sharded-results.sh"):
        src = project_root / "terminal" / name
        dest = root / name
        if not src.is_file():
            raise RuntimeError(f"missing OpenAPI build support file: {src}")
        if not dest.exists() or dest.read_bytes() != src.read_bytes():
            dest.write_bytes(src.read_bytes())


def patch_dockerfile(root: Path) -> None:
    _copy_support_files(root)
    path = root / "Dockerfile"
    _replace_once(path, "ARG ELB_REF=dad3943\n", "ARG ELB_REF=7a471297\n")
    _insert_once(
        path,
        "COPY ./app /app\n",
        (
            "COPY patch_elastic_blast.py /tmp/patch_elastic_blast.py\n"
            "COPY merge-sharded-results.sh /tmp/merge-sharded-results.sh\n"
        ),
        "COPY patch_elastic_blast.py /tmp/patch_elastic_blast.py",
    )
    _insert_once(
        path,
        "    git -C /tmp/elb-src checkout ${ELB_REF} && \\\n",
        "    python3 /tmp/patch_elastic_blast.py /tmp/elb-src /tmp/merge-sharded-results.sh && \\\n",
        "python3 /tmp/patch_elastic_blast.py /tmp/elb-src",
    )
    _replace_once(
        path,
        "    rm -rf /tmp/elb-src && \\\n",
        "    true && \\\n",
    )
    _replace_once(
        path,
        "    pip3 install --no-cache-dir --no-build-isolation /tmp/elb-src && \\\n",
        (
            "    pip3 install --no-cache-dir --no-build-isolation /tmp/elb-src && \\\n"
            "    cp -a /tmp/elb-src/src/elastic_blast/templates/. /usr/local/lib/python3.11/site-packages/elastic_blast/templates/ && \\\n"
        ),
    )
    _replace_once(
        path,
        "    && pip install --no-cache-dir azure-cli \\\n",
        (
            "    && pip install --no-cache-dir azure-cli \\\n"
            "    && pip install --no-cache-dir --no-deps --no-build-isolation /tmp/elb-src \\\n"
            "    && cp -a /tmp/elb-src/src/elastic_blast/templates/. /opt/venv/lib/python3.11/site-packages/elastic_blast/templates/ \\\n"
            "    && rm -rf /tmp/elb-src \\\n"
        ),
    )


def patch_app(root: Path) -> None:
    path = root / "app" / "main.py"
    _replace_once(
        path,
        "    return None\n\n\ndef _ensure_elb_scripts_configmap() -> None:\n",
        (
            "    return None\n\n\n"
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
            '        "\\n".join(\n'
            '            str(job_info.get(key) or "")\n'
            '            for key in ("stdout_tail", "stderr_tail")\n'
            "        ),\n"
            "    )\n"
            "    if discovered:\n"
            "        _update_job(job_id, elb_job_id=discovered)\n"
            "        return discovered\n"
            "    return current or job_id\n"
            "\n\n"
            "def _ensure_elb_scripts_configmap() -> None:\n"
        ),
    )
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
    _replace_once(
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
