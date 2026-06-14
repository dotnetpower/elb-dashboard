#!/usr/bin/env python3
# ruff: noqa: E501
"""Patch the sibling docker-openapi build context for dashboard runtime policy.

Responsibility: Patch the sibling docker-openapi build context for dashboard runtime policy
Edit boundaries: Keep this as an operator/dev utility; do not make production code depend on it.
Key entry points: `_replace_once`, `_insert_once`, `_copy_support_files`, `patch_dockerfile`,
`patch_app`, `main`
Risky contracts: Assume local developer context only; avoid broad production-side effects.
Validation: `uv run python scripts/dev/patch-openapi-build-context.py --help`.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if new in text:
        # Idempotent re-run: the final form of this replacement is already
        # present in the file. This tolerates the sibling Dockerfile / app
        # catching up to upstream (e.g. ``ARG ELB_REF`` advancing past the
        # value we used to inject, OR the venv-stage block being added
        # natively upstream so the dashboard insertion would otherwise
        # duplicate it).
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
    _copy_app_overlay(root)
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
