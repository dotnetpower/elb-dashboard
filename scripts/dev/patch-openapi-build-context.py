#!/usr/bin/env python3
# ruff: noqa: E501
"""Patch the sibling docker-openapi build context for dashboard runtime policy.

Responsibility: Patch the sibling docker-openapi build context for dashboard runtime policy
Edit boundaries: Keep this as an operator/dev utility; do not make production code depend on it.
Key entry points: `_replace_once`, `_insert_once`, `_copy_support_files`,
`_ensure_reference_context_dependency`, `patch_dockerfile`,
`_disable_warmed_cache_skip`, `_patch_canonical_merged_result_validation`,
`_patch_partitioned_completion_fail_closed`, `_patch_result_selection_policy`,
`_patch_finalizer_failure_status`,
`_patch_web_blast_candidate_selection_evidence`, `_harden_openapi_runtime_ids`,
`_patch_submit_runtime_id_priority`, `_harden_elb_scripts_configmap_reconciliation`,
`patch_app`, `main`
Risky contracts: Preserve strict result-path validation; only shard outputs and the exact canonical
merged filename may pass. Precise core_nt submits without an explicit search space must derive it
from validated active-generation metadata and fail closed when that metadata is unavailable. Assume
local developer context only; avoid broad production-side effects.
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
        source_bytes = src.read_bytes()
        if not dest.exists() or dest.read_bytes() != source_bytes:
            dest.write_bytes(source_bytes)


def _is_tracked_sibling_module(root: Path, path: Path) -> bool:
    """Return whether ``path`` is native source tracked by the sibling clone."""
    import shutil
    import subprocess

    try:
        relative = path.relative_to(root).as_posix()
        git = shutil.which("git")
        if not git:
            return False
        result = subprocess.run(  # noqa: S603 - resolved executable and fixed argv.
            [git, "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _copy_app_overlay(root: Path) -> None:
    """Copy dashboard runtime overlays into the build-context ``app/``.

    The Dockerfile already ``COPY ./app /app`` so dropping modules next to
    ``main.py`` makes their imports resolve at runtime.
    """
    project_root = Path(__file__).resolve().parents[2]
    for name in ("eta.py", "exact_oracle.py", "reference_context.py"):
        src = project_root / "scripts" / "dev" / "openapi-overlays" / name
        if not src.is_file():
            raise RuntimeError(f"missing OpenAPI overlay: {src}")
        dest = root / "app" / name
        if name in {"exact_oracle.py", "reference_context.py"} and dest.is_file():
            if _is_tracked_sibling_module(root, dest):
                continue
        if not dest.exists() or dest.read_bytes() != src.read_bytes():
            dest.write_bytes(src.read_bytes())


def _ensure_reference_context_dependency(root: Path) -> None:
    """Pin the hardened XML parser used by the reference-context overlay."""
    path = root / "app" / "requirements.txt"
    lines = path.read_text().splitlines()
    operators = ("==", ">=", "<=", "~=", "!=", ">", "<", "[", " @", ";")

    def is_defusedxml_requirement(line: str) -> bool:
        requirement = line.split("#", 1)[0].strip().lower()
        return requirement == "defusedxml" or any(
            requirement.startswith(f"defusedxml{operator}") for operator in operators
        )

    matches = [index for index, line in enumerate(lines) if is_defusedxml_requirement(line)]
    if len(matches) > 1:
        raise RuntimeError(f"duplicate defusedxml requirements in {path}")
    pinned = "defusedxml==0.7.1"
    if matches:
        lines[matches[0]] = pinned
    else:
        lines.append(pinned)
    desired = "\n".join(lines) + "\n"
    if path.read_text() != desired:
        path.write_text(desired)
    pinned_count = sum(
        line.split("#", 1)[0].strip().lower() == pinned
        for line in path.read_text().splitlines()
    )
    if pinned_count != 1:
        raise RuntimeError(f"expected one pinned defusedxml requirement in {path}")


def _validate_copied_runtime_policy(root: Path) -> None:
    """Verify native or copied runtime helpers retain required safety contracts."""
    required = {
        root / "app" / "result_selection.py": (
            "SEQUENCE_DIVERSITY_DEFAULT_CANDIDATE_POOL_SIZE = 2_000",
            "if applied_pool <= 0:",
            "def prepare_sequence_diversity_options(",
        ),
        root / "app" / "exact_oracle.py": (
            "def _context_nonnegative_int(",
            "One-shard manifest exceeds the volume limit",
            "oracle_source != source_version",
        ),
        root / "app" / "reference_context.py": (
            "from defusedxml import ElementTree as ET",
            "active_total_letters,",
            "deepcopy(cached[1])",
            "_FETCH_LOCK.acquire(timeout=_FETCH_LOCK_WAIT_SECONDS)",
        ),
        root / "merge-sharded-results.sh": (
            "num_shards must be between 1 and 1024",
            "SEQUENCE_GROUP_REPORT_LIMIT = 5_000",
            "def sequence_diversity_representatives(",
            "len(observed_source_shards) == expected_shards",
        ),
    }
    forbidden = {
        root / "app" / "result_selection.py": (
            "SEQUENCE_DIVERSITY_MAX_CANDIDATE_POOL_SIZE",
        ),
        root / "merge-sharded-results.sh": (
            "SEQUENCE_DIVERSITY_MAX_CANDIDATE_POOL_SIZE",
            "candidate pool cannot exceed 5000",
        ),
    }
    missing: list[str] = []
    for path, fragments in required.items():
        if not path.is_file():
            missing.append(f"missing file {path}")
            continue
        text = path.read_text()
        missing.extend(
            f"{path}: {fragment}" for fragment in fragments if fragment not in text
        )
        missing.extend(
            f"{path}: forbidden {fragment}"
            for fragment in forbidden.get(path, ())
            if fragment in text
        )
    requirements = (root / "app" / "requirements.txt").read_text().splitlines()
    if requirements.count("defusedxml==0.7.1") != 1:
        missing.append("app/requirements.txt: exactly one defusedxml==0.7.1")
    if missing:
        raise RuntimeError("OpenAPI copied runtime policy mismatch: " + "; ".join(missing))


def _patch_external_soft_masking(root: Path) -> None:
    """Keep external XML filtering/statistics equal to dashboard submit."""
    schemas = root / "app" / "schemas.py"
    main = root / "app" / "main.py"
    _insert_once(
        schemas,
        '    extra: Optional[str] = Field(None, description="Additional BLAST CLI options as raw string.")\n',
        (
            "    db_effective_search_space: Optional[int] = Field(\n"
            "        None,\n"
            "        ge=1,\n"
            "        description=(\n"
            '            "Optional explicit scoring search space. Omit for core_nt so the "\n'
            '            "server derives it from the active database generation."\n'
            "        ),\n"
            "    )\n"
        ),
        "Optional explicit scoring search space. Omit for core_nt",
    )
    _insert_once(
        schemas,
        '    extra: Optional[str] = Field(None, description="Additional BLAST CLI options as raw string.")\n',
        (
            "    result_selection_policy: Literal[\n"
            '        "native_top_n", "diversity_aware", "sequence_diversity"\n'
            "    ] = Field(\n"
            '        "native_top_n",\n'
            "        description=(\n"
            '            "Final subject-selection policy. native_top_n reproduces the "\n'
            '            "BLAST top-N comparator; diversity_aware reserves lower-score "\n'
            '            "subjects when a tied score class fills the result window; "\n'
            '            "sequence_diversity selects one representative per aligned subject "\n'
            '            "sequence and query span for tabular output."\n'
            "        ),\n"
            "    )\n"
        ),
        "Final subject-selection policy. native_top_n reproduces the",
    )
    _insert_once(
        schemas,
        '    extra: Optional[str] = Field(None, description="Additional BLAST CLI options as raw string.")\n',
        (
            '    web_blast_statistical_context: Optional["WebBlastStatisticalContext"] = Field(\n'
            "        None,\n"
            "        description=(\n"
            '            "Optional measured single-query taxonomy-filtered statistics; "\n'
            '            "omit when these reference values are unavailable."\n'
            "        ),\n"
            "    )\n"
        ),
        "Optional measured single-query taxonomy-filtered statistics",
    )
    _insert_once(
        main,
        "        if opts.extra: parts.append(opts.extra)\n",
        (
            "        if opts.db_effective_search_space is not None:\n"
            '            raw_extra = str(opts.extra or "")\n'
            '            if re.search(r"(?<!\\S)-(?:searchsp|dbsize)(?:\\s|=|$)", raw_extra):\n'
            "                raise HTTPException(\n"
            "                    400,\n"
            '                    "blast_options.db_effective_search_space conflicts with "\n'
            '                    "blast_options.extra -searchsp/-dbsize",\n'
            "                )\n"
            '            parts.append("-searchsp " + str(opts.db_effective_search_space))\n'
        ),
        'parts.append("-searchsp " + str(opts.db_effective_search_space))',
    )
    _replace_once_unless_marker(
        schemas,
        "class ExternalBlastOptions(BaseModel):\n",
        (
            "class WebBlastStatisticalContext(BaseModel):\n"
            "    filtered_database_letters: int = Field(..., ge=1)\n"
            "    filtered_database_sequences: int = Field(..., ge=1)\n"
            "    length_adjustment: int = Field(..., ge=0)\n"
            "    effective_search_space: int = Field(..., ge=1)\n"
            "    scoring_search_space: int = Field(..., ge=1)\n"
            "    result_database_letters: int = Field(..., ge=1)\n\n\n"
            "class ExternalBlastOptions(BaseModel):\n"
        ),
        "class WebBlastStatisticalContext(BaseModel):",
    )
    _insert_once(
        schemas,
        "    dust: bool = Field(True)\n",
        "    soft_masking: bool = Field(False)\n",
        "soft_masking: bool = Field(False)",
    )
    _insert_once(
        schemas,
        "    soft_masking: bool = Field(False)\n",
        "    db_effective_search_space: Optional[int] = Field(None, ge=1)\n",
        "db_effective_search_space: Optional[int] = Field(None, ge=1)",
    )
    _insert_once(
        schemas,
        "    db_effective_search_space: Optional[int] = Field(None, ge=1)\n",
        ("    web_blast_statistical_context: Optional[WebBlastStatisticalContext] = None\n"),
        "web_blast_statistical_context: Optional[WebBlastStatisticalContext]",
    )
    _insert_once(
        main,
        '        "-dust yes" if opts.dust else "-dust no",\n',
        ('        "-soft_masking true" if opts.soft_masking else "-soft_masking false",\n'),
        '"-soft_masking true" if opts.soft_masking',
    )
    _insert_once(
        main,
        ('        "-soft_masking true" if opts.soft_masking else "-soft_masking false",\n    ]\n'),
        (
            "    if opts.db_effective_search_space is not None:\n"
            '        parts.append(f"-searchsp {opts.db_effective_search_space}")\n'
        ),
        'parts.append(f"-searchsp {opts.db_effective_search_space}")',
    )
    _insert_once(
        main,
        '        parts.append(f"-searchsp {opts.db_effective_search_space}")\n',
        (
            "    if opts.web_blast_statistical_context is not None:\n"
            "        parts.append(\n"
            '            f"-dbsize {opts.web_blast_statistical_context.filtered_database_letters}"\n'
            "        )\n"
        ),
        "opts.web_blast_statistical_context.filtered_database_letters",
    )
    fresh_bridge = (
        '        extra=f"-word_size {req.options.word_size} '
        "{'-dust yes' if req.options.dust else '-dust no'}\",\n"
    )
    legacy_bridge = (
        "        extra=(\n"
        '            f"-word_size {req.options.word_size} "\n'
        "            f\"{'-dust yes' if req.options.dust else '-dust no'} \"\n"
        "            f\"{'-soft_masking true' if req.options.soft_masking else '-soft_masking false'}\"\n"
        "        ),\n"
    )
    desired_bridge = (
        "        extra=(\n"
        '            f"-word_size {req.options.word_size} "\n'
        "            f\"{'-dust yes' if req.options.dust else '-dust no'} \"\n"
        "            f\"{'-soft_masking true' if req.options.soft_masking else '-soft_masking false'}\"\n"
        "            + (\n"
        '                f" -searchsp {req.options.db_effective_search_space}"\n'
        "                if req.options.db_effective_search_space is not None\n"
        '                else ""\n'
        "            )\n"
        "            + (\n"
        '                f" -dbsize {req.options.web_blast_statistical_context.filtered_database_letters}"\n'
        "                if req.options.web_blast_statistical_context is not None\n"
        '                else ""\n'
        "            )\n"
        "        ),\n"
    )
    _replace_fresh_or_legacy(
        main,
        fresh=fresh_bridge,
        legacy=legacy_bridge,
        desired=desired_bridge,
        marker="req.options.web_blast_statistical_context is not None",
    )
    _insert_once(
        main,
        "    internal = JobSubmitRequest(\n",
        (
            "        web_blast_statistical_context=(\n"
            "            req.options.web_blast_statistical_context.model_dump()\n"
            "            if req.options.web_blast_statistical_context is not None\n"
            "            else None\n"
            "        ),\n"
        ),
        "web_blast_statistical_context=(",
    )
    schema_text = schemas.read_text()
    main_text = main.read_text()
    if schema_text.count("soft_masking: bool = Field(False)") != 1:
        raise RuntimeError("external soft-masking schema patch is missing or duplicated")
    if schema_text.count("db_effective_search_space: Optional[int]") != 2:
        raise RuntimeError("search-space schema patches are missing or duplicated")
    if (
        schema_text.count("    result_selection_policy: Literal[") != 1
        or '"sequence_diversity"' not in schema_text
    ):
        raise RuntimeError("result-selection policy schema patch is missing or duplicated")
    if schema_text.count("Optional measured single-query taxonomy-filtered statistics") != 1:
        raise RuntimeError("direct Web statistics schema patch is missing or duplicated")
    if schema_text.count("class WebBlastStatisticalContext(BaseModel):") != 1:
        raise RuntimeError("external Web BLAST statistics schema patch is missing or duplicated")
    if main_text.count("req.options.soft_masking") != 1:
        raise RuntimeError("external soft-masking bridge patch is missing or duplicated")
    if main_text.count("opts.soft_masking") != 1:
        raise RuntimeError("external soft-masking option patch is missing or duplicated")
    if main_text.count("if opts.db_effective_search_space is not None:") != 2:
        raise RuntimeError("search-space option guards are missing or duplicated")
    if main_text.count('parts.append("-searchsp " + str(opts.db_effective_search_space))') != 1:
        raise RuntimeError("direct search-space option patch is missing or duplicated")
    if main_text.count('parts.append(f"-searchsp {opts.db_effective_search_space}")') != 1:
        raise RuntimeError("external search-space option patch is missing or duplicated")
    if main_text.count("req.options.db_effective_search_space") != 2:
        raise RuntimeError("external search-space bridge patch is missing or duplicated")
    if main_text.count("req.options.web_blast_statistical_context") != 4:
        raise RuntimeError("external Web BLAST statistics bridge patch is missing or duplicated")


def _patch_openapi_response_schemas(root: Path) -> None:
    """Publish additive readiness, selection, and reference-resolver schemas."""
    schemas = root / "app" / "schemas.py"
    main = root / "app" / "main.py"
    _replace_once_unless_marker(
        schemas,
        "class ExternalBlastOptions(BaseModel):\n",
        (
            "class WebBlastStatisticalContextRequest(BaseModel):\n"
            '    rid: str = Field(..., min_length=8, max_length=16, pattern=r"^[A-Z0-9]{8,16}$")\n'
            "    query_fasta: str = Field(..., min_length=1, max_length=10_000_000)\n"
            '    db: Literal["core_nt"] = "core_nt"\n'
            "    taxid: Optional[int] = Field(None, ge=1, le=2_147_483_647)\n"
            "    is_inclusive: Optional[bool] = Field(\n"
            "        None,\n"
            '        description="With taxid, true includes the taxon and false excludes it; omitted defaults to true.",\n'
            "    )\n"
            "\n\n"
            "class WebBlastStatisticalContextResponse(BaseModel):\n"
            '    status: Literal["resolved"]\n'
            "    rid: str\n"
            '    database: Literal["core_nt"]\n'
            "    reference_query_id: str\n"
            "    submitted_query_id: str\n"
            "    query_length: int = Field(..., ge=1)\n"
            "    active_source_version: str\n"
            "    web_blast_statistical_context: WebBlastStatisticalContext\n"
            "    query_effective_search_spaces: list[int]\n"
            "    expected_filter: dict[str, Any]\n"
            "    evidence: dict[str, Any]\n"
            "    warnings: list[str]\n"
            "\n\n"
            "class JobStatusResponse(BaseModel):\n"
            '    model_config = {"extra": "allow"}\n'
            "\n"
            "    job_id: str\n"
            "    status: str\n"
            "    phase: Optional[str] = None\n"
            "    results_ready: Optional[bool] = None\n"
            "    results_ready_at: Optional[str] = None\n"
            "    merged_at: Optional[str] = None\n"
            "    db_partitions: Optional[int] = Field(None, ge=0)\n"
            "    result_selection_policy: Optional[\n"
            '        Literal["native_top_n", "diversity_aware", "sequence_diversity"]\n'
            "    ] = None\n"
            '    sequence_identity_mode: Optional[Literal["aligned_sequence_query_span"]] = None\n'
            "    sequence_identity_version: Optional[int] = Field(None, ge=1)\n"
            "    requested_sequence_groups: Optional[int] = Field(None, ge=1)\n"
            "    candidate_pool_size_requested_per_shard: Optional[int] = Field(None, ge=1)\n"
            "    candidate_pool_size_applied_per_shard: Optional[int] = Field(None, ge=1)\n"
            "\n\n"
            "class JobListResponse(BaseModel):\n"
            '    model_config = {"extra": "allow"}\n'
            "\n"
            "    jobs: list[JobStatusResponse]\n"
            "    count: int = Field(..., ge=0)\n"
            "    next_cursor: Optional[str] = None\n"
            "    has_more: bool = False\n"
            "\n\n"
            "class ExternalBlastOptions(BaseModel):\n"
        ),
        "class WebBlastStatisticalContextRequest(BaseModel):",
    )
    _insert_once(
        main,
        "    JobSubmitRequest,\n",
        (
            "    JobListResponse,\n"
            "    JobStatusResponse,\n"
            "    WebBlastStatisticalContextRequest,\n"
            "    WebBlastStatisticalContextResponse,\n"
        ),
        "    WebBlastStatisticalContextResponse,\n",
    )
    _replace_once_unless_marker(
        main,
        '@v1.get("/jobs", tags=["Jobs"], summary="List all jobs")\n',
        (
            '@v1.get(\n'
            '    "/jobs",\n'
            '    tags=["Jobs"],\n'
            '    summary="List all jobs",\n'
            '    response_model=JobListResponse,\n'
            ')\n'
        ),
        "response_model=JobListResponse",
    )
    _replace_once_unless_marker(
        main,
        '@v1.get("/jobs/{job_id}/status", tags=["Jobs"], summary="Get job status")\n',
        (
            '@v1.get(\n'
            '    "/jobs/{job_id}/status",\n'
            '    tags=["Jobs"],\n'
            '    summary="Get job status",\n'
            '    response_model=JobStatusResponse,\n'
            ')\n'
        ),
        "response_model=JobStatusResponse",
    )
    _replace_once_unless_marker(
        main,
        '@external_v1.post("/submit", status_code=202, summary="Submit an external ElasticBLAST job")\n',
        (
            '@external_v1.post(\n'
            '    "/submit",\n'
            '    status_code=202,\n'
            '    summary="Submit an external ElasticBLAST job",\n'
            '    response_model=JobStatusResponse,\n'
            ')\n'
        ),
        'summary="Submit an external ElasticBLAST job",\n    response_model=JobStatusResponse,',
    )
    _replace_once_unless_marker(
        main,
        '@external_v1.get("/jobs/{job_id}", summary="Get external ElasticBLAST job status")\n',
        (
            '@external_v1.get(\n'
            '    "/jobs/{job_id}",\n'
            '    summary="Get external ElasticBLAST job status",\n'
            '    response_model=JobStatusResponse,\n'
            ')\n'
        ),
        'summary="Get external ElasticBLAST job status",\n    response_model=JobStatusResponse,',
    )


def _patch_reference_context_endpoint(root: Path) -> None:
    """Add the authenticated RID-to-statistical-context resolver endpoint."""
    main = root / "app" / "main.py"
    _insert_once(
        main,
        "from util import run_cancellable, safe_exec\n",
        "import reference_context as _reference_context\n",
        "import reference_context as _reference_context",
    )
    submit_anchor = (
        "# ── Jobs — Submit ──────────────────────────────────────────────────────────\n"
    )
    route = (
        '@v1.post(\n'
        '    "/web-blast/statistical-context",\n'
        '    tags=["Jobs"],\n'
        '    summary="Resolve Web BLAST statistical context from an NCBI RID",\n'
        '    response_model=WebBlastStatisticalContextResponse,\n'
        ')\n'
        "def resolve_web_blast_statistical_context(\n"
        "    req: WebBlastStatisticalContextRequest,\n"
        ") -> dict[str, Any]:\n"
        "    if req.taxid is None and req.is_inclusive is not None:\n"
        '        raise HTTPException(422, "is_inclusive requires taxid")\n'
        "    if _exact_oracle is None:\n"
        '        raise HTTPException(503, "Active database metadata support is unavailable")\n'
        "    try:\n"
        "        active_database = _exact_oracle.read_active_database(\n"
        "            blob_base=_blob_base(),\n"
        "            db_name=req.db,\n"
        "            token=_storage_oauth_token(),\n"
        "        )\n"
        "    except Exception as exc:\n"
        "        raise HTTPException(\n"
        "            503,\n"
        '            detail={"code": "active_database_unavailable", "message": "Active database generation metadata is unavailable", "retryable": True},\n'
        "        ) from exc\n"
        "    try:\n"
        "        return _reference_context.resolve_reference_context(\n"
        "            rid=req.rid,\n"
        "            query_fasta=req.query_fasta,\n"
        "            active_total_letters=active_database.total_letters,\n"
        "            active_total_sequences=active_database.total_sequences,\n"
        "            active_source_version=active_database.source_version,\n"
        "            taxid=req.taxid,\n"
        "            is_inclusive=(True if req.taxid is not None and req.is_inclusive is None else req.is_inclusive),\n"
        "        )\n"
        "    except _reference_context.ReferenceContextNotReady as exc:\n"
        "        raise HTTPException(\n"
        "            409,\n"
        '            detail={"code": "reference_not_ready", "message": str(exc), "retryable": True},\n'
        '            headers={"Retry-After": "30"},\n'
        "        ) from exc\n"
        "    except _reference_context.ReferenceContextUnavailable as exc:\n"
        "        raise HTTPException(\n"
        "            503,\n"
        '            detail={"code": "reference_unavailable", "message": str(exc), "retryable": True},\n'
        "        ) from exc\n"
        "    except _reference_context.ReferenceContextError as exc:\n"
        "        raise HTTPException(\n"
        "            422,\n"
        '            detail={"code": "reference_invalid", "message": str(exc), "retryable": False},\n'
        "        ) from exc\n"
        "\n\n"
    )
    legacy_lookup_boundary = (
        "            token=_storage_oauth_token(),\n"
        "        )\n"
        "        return _reference_context.resolve_reference_context(\n"
    )
    hardened_lookup_boundary = (
        "            token=_storage_oauth_token(),\n"
        "        )\n"
        "    except Exception as exc:\n"
        "        raise HTTPException(\n"
        "            503,\n"
        '            detail={"code": "active_database_unavailable", "message": "Active database generation metadata is unavailable", "retryable": True},\n'
        "        ) from exc\n"
        "    try:\n"
        "        return _reference_context.resolve_reference_context(\n"
    )
    main_text = main.read_text()
    if (
        "def resolve_web_blast_statistical_context(" in main_text
        and '"active_database_unavailable"' not in main_text
    ):
        _replace_once(main, legacy_lookup_boundary, hardened_lookup_boundary)
    _insert_once(
        main,
        submit_anchor,
        route,
        'def resolve_web_blast_statistical_context(',
    )
    metadata_error = (
        "    except Exception as exc:\n"
        "        raise HTTPException(\n"
        "            503,\n"
        '            detail={"code": "active_database_unavailable", "message": "Active database generation metadata is unavailable", "retryable": True},\n'
        "        ) from exc\n"
    )
    metadata_error_with_log = (
        "    except Exception as exc:\n"
        "        logger.warning(\n"
        '            "reference context active database metadata unavailable error_type=%s",\n'
        "            type(exc).__name__,\n"
        "        )\n"
        "        raise HTTPException(\n"
        "            503,\n"
        '            detail={"code": "active_database_unavailable", "message": "Active database generation metadata is unavailable", "retryable": True},\n'
        "        ) from exc\n"
    )
    _replace_once_unless_marker(
        main,
        metadata_error,
        metadata_error_with_log,
        "reference context active database metadata unavailable error_type=%s",
    )


def _replace_stale_core_nt_search_space_fallback(path: Path) -> None:
    """Pin active DB paths and derive a missing search space from active metadata."""
    required_search_space_guard = (
        '        if "-searchsp" not in opts and "-dbsize" not in opts:\n'
        "            raise HTTPException(\n"
        "                400,\n"
        '                "Precise core_nt sharding requires db_effective_search_space",\n'
        "            )\n"
    )
    # OpenAPI 4.38 briefly required callers to provide the scalar even though
    # the active-generation block below already derives a safe fallback.
    # Remove that legacy guard before matching either patched source shape.
    _replace_once(path, required_search_space_guard, "")
    fallback_only = (
        '        if "-searchsp" not in opts and "-dbsize" not in opts:\n'
        '            config["blast"]["options"] = f"{opts} -searchsp 32156241807668"\n'
    )
    fresh = (
        "        partitions = max(1, min(NUM_NODES, 10))\n"
        '        config["blast"]["db-partitions"] = str(partitions)\n'
        '        config["blast"]["db-partition-prefix"] = (\n'
        '            f"{_blob_base()}/blast-db/{partitions}shards/core_nt_shard_"\n'
        "        )\n" + fallback_only
    )
    legacy = (
        "        partitions = max(1, min(NUM_NODES, 10))\n"
        '        config["blast"]["db-partitions"] = str(partitions)\n'
        '        config["blast"]["db-partition-prefix"] = (\n'
        '            f"{_blob_base()}/blast-db/{partitions}shards/core_nt_shard_"\n'
        "        )\n"
        "        try:\n"
        "            active_database = _exact_oracle.read_active_database(\n"
        "                blob_base=_blob_base(),\n"
        "                db_name=db_name,\n"
        "                token=_storage_oauth_token(),\n"
        "            )\n"
        "            opts, web_blast_statistics = _exact_oracle.prepare_web_blast_statistics(\n"
        '                context=(req.model_extra or {}).get("web_blast_statistical_context"),\n'
        '                query_fasta=str(req.query_fasta or ""),\n'
        "                active_database=active_database,\n"
        "                options=opts,\n"
        "            )\n"
        "            if web_blast_statistics is None:\n"
        "                opts = _exact_oracle.preserve_or_set_search_space(\n"
        "                    opts, active_database.search_space\n"
        "                )\n"
        '            config["blast"]["db"] = (\n'
        '                f"{_blob_base()}/blast-db/{active_database.db_prefix}"\n'
        "            )\n"
        '            config["blast"]["db-partition-prefix"] = (\n'
        '                f"{_blob_base()}/blast-db/{active_database.shard_layout_prefix}/"\n'
        '                f"{partitions}shards/{db_name}_shard_"\n'
        "            )\n"
        '            config["blast"]["options"] = opts\n'
        "        except Exception as exc:\n"
        "            logger.warning(\n"
        '                "active DB search-space resolution failed db=%s reason=%s",\n'
        "                db_name,\n"
        "                type(exc).__name__,\n"
        "            )\n"
        "            raise HTTPException(\n"
        "                503,\n"
        '                "Active database statistics are required for precise core_nt sharding",\n'
        "            ) from exc\n"
    )
    desired = (
        "        default_partitions = max(1, min(NUM_NODES, 10))\n"
        "        one_shard_layout = None\n"
        "        try:\n"
        "            active_database = _exact_oracle.read_active_database(\n"
        "                blob_base=_blob_base(),\n"
        "                db_name=db_name,\n"
        "                token=_storage_oauth_token(),\n"
        "            )\n"
        "            opts, web_blast_statistics = _exact_oracle.prepare_web_blast_statistics(\n"
        '                context=(req.model_extra or {}).get("web_blast_statistical_context"),\n'
        '                query_fasta=str(req.query_fasta or ""),\n'
        "                active_database=active_database,\n"
        "                options=opts,\n"
        "            )\n"
        "            partitions = _exact_oracle.select_web_blast_partitions(\n"
        "                web_blast_statistics,\n"
        "                default_partitions=default_partitions,\n"
        "            )\n"
        "            if web_blast_statistics is not None and partitions == 1:\n"
        "                one_shard_layout = _exact_oracle.read_one_shard_layout(\n"
        "                    blob_base=_blob_base(),\n"
        "                    db_name=db_name,\n"
        "                    active_database=active_database,\n"
        "                    token=_storage_oauth_token(),\n"
        "                )\n"
        "            if web_blast_statistics is None:\n"
        "                opts = _exact_oracle.preserve_or_set_search_space(\n"
        "                    opts, active_database.search_space\n"
        "                )\n"
        '            config["blast"]["db-partitions"] = str(partitions)\n'
        "            if web_blast_statistics is not None and partitions == 1:\n"
        '                config["blast"]["disk-backed-monolithic"] = "true"\n'
        '                config["blast"]["mem-request"] = "104Gi"\n'
        '                config["blast"]["mem-limit"] = "112Gi"\n'
        '            config["blast"]["db"] = (\n'
        '                f"{_blob_base()}/blast-db/{active_database.db_prefix}"\n'
        "            )\n"
        '            config["blast"]["db-partition-prefix"] = (\n'
        '                f"{_blob_base()}/blast-db/{active_database.shard_layout_prefix}/"\n'
        '                f"{partitions}shards/{db_name}_shard_"\n'
        "            )\n"
        '            config["blast"]["options"] = opts\n'
        "        except Exception as exc:\n"
        "            logger.warning(\n"
        '                "active DB search-space resolution failed db=%s reason=%s",\n'
        "                db_name,\n"
        "                type(exc).__name__,\n"
        "            )\n"
        "            raise HTTPException(\n"
        "                503,\n"
        '                "Active database statistics are required for precise core_nt sharding",\n'
        "            ) from exc\n"
    )
    text = path.read_text()
    if "select_web_blast_partitions(" not in text and fresh not in text and legacy not in text:
        _replace_once(path, fallback_only, desired)
    else:
        _replace_fresh_or_legacy(
            path,
            fresh=fresh,
            legacy=legacy,
            desired=desired,
            marker="select_web_blast_partitions(",
        )
    text = path.read_text()
    if (
        fresh in text
        or legacy in text
        or fallback_only in text
        or text.count("select_web_blast_partitions(") != 1
        or text.count("preserve_or_set_search_space(") != 1
        or text.count("Active database statistics are required for precise core_nt sharding") != 1
    ):
        raise RuntimeError("precise core_nt active search-space patch is invalid")


def _patch_web_blast_candidate_selection_evidence(path: Path) -> None:
    """Record whether exact execution used monolithic or partitioned candidates."""
    anchor = "            ).as_dict()\n            if web_blast_statistics is not None:\n"
    legacy = (
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
    )
    legacy_with_layout = (
        legacy.removesuffix("            if web_blast_statistics is not None:\n")
        + "            if one_shard_layout is not None:\n"
        "                exact_oracle_info.update(one_shard_layout.as_dict())\n"
        "            if web_blast_statistics is not None:\n"
    )
    execution_evidence = (
        "            if web_blast_statistics is not None:\n"
        "                exact_oracle_info.update(\n"
        "                    _exact_oracle.validate_web_blast_execution_options(\n"
        "                        opts, program=req.program\n"
        "                    )\n"
        "                )\n"
    )
    desired = (
        "            ).as_dict()\n"
        "            exact_oracle_info.update(\n"
        "                {\n"
        '                    "candidate_selection": (\n'
        '                        "monolithic_full_database"\n'
        "                        if partitions == 1\n"
        '                        else "partitioned_shards"\n'
        "                    ),\n"
        '                    "db_partitions": partitions,\n'
        '                    "memory_mode": (\n'
        '                        "disk_backed_bounded"\n'
        "                        if web_blast_statistics is not None and partitions == 1\n"
        '                        else "memory_cached_shards"\n'
        "                    ),\n"
        '                    "memory_request": "104Gi" if partitions == 1 else None,\n'
        '                    "memory_limit": "112Gi" if partitions == 1 else None,\n'
        "                }\n"
        "            )\n"
        "            if one_shard_layout is not None:\n"
        "                exact_oracle_info.update(one_shard_layout.as_dict())\n"
        + execution_evidence
        + "            if web_blast_statistics is not None:\n"
    )
    legacy_with_memory = desired.replace(execution_evidence, "", 1)
    text = path.read_text()
    if "opts, program=req.program" in text:
        return
    source = next(
        (
            candidate
            for candidate in (legacy_with_memory, legacy_with_layout, legacy, anchor)
            if candidate in text
        ),
        None,
    )
    if source is None or text.count(source) != 1:
        raise RuntimeError(f"expected one candidate-selection evidence block in {path}")
    path.write_text(text.replace(source, desired, 1))


def _patch_canonical_merged_result_discovery(path: Path) -> None:
    """Expose a partitioned run's merged XML instead of shard intermediates."""
    text = path.read_text()
    signature = next(
        (
            candidate
            for candidate in (
                "def _list_result_files(job_info: dict[str, Any]) -> list[dict[str, Any]]:\n",
                "def _list_result_files(job_info):\n",
            )
            if candidate in text
        ),
        "def _list_result_files(job_info):\n",
    )
    _replace_once_unless_marker(
        path,
        signature,
        (
            "def _result_partition_count(job_info):\n"
            '    exact_oracle = job_info.get("exact_oracle")\n'
            "    raw_partitions = (\n"
            '        job_info.get("db_partitions")\n'
            '        or exact_oracle.get("db_partitions", 0)\n'
            "        if isinstance(exact_oracle, dict)\n"
            '        else job_info.get("db_partitions", 0)\n'
            "    )\n"
            "    try:\n"
            "        return max(0, int(raw_partitions or 0))\n"
            "    except (TypeError, ValueError):\n"
            "        return 0\n"
            "\n\n" + signature
        ),
        "def _result_partition_count(job_info):",
    )
    legacy_partition_block = (
        '    exact_oracle = job_info.get("exact_oracle")\n'
        "    result_partitions = (\n"
        '        job_info.get("db_partitions")\n'
        '        or exact_oracle.get("db_partitions", 0)\n'
        "        if isinstance(exact_oracle, dict)\n"
        '        else job_info.get("db_partitions", 0)\n'
        "    )\n"
        "    try:\n"
        "        requires_merged_result = int(result_partitions or 0) > 1\n"
        "    except (TypeError, ValueError):\n"
        "        requires_merged_result = False\n"
    )
    desired_partition_line = "    requires_merged_result = _result_partition_count(job_info) > 1\n"
    text = path.read_text()
    if legacy_partition_block in text:
        path.write_text(text.replace(legacy_partition_block, desired_partition_line, 1))
    else:
        _insert_once(
            path,
            signature,
            desired_partition_line,
            desired_partition_line.strip(),
        )
    legacy_existing = (
        '    existing = job_info.get("result_files")\n'
        "    if isinstance(existing, list) and existing:\n"
        "        return existing\n"
    )
    desired_existing = (
        '    existing = job_info.get("result_files")\n'
        "    if isinstance(existing, list) and existing:\n"
        '        if any(item.get("filename") == "merged_results.out.gz" for item in existing):\n'
        "            return existing\n"
        "        if not requires_merged_result:\n"
        "            return existing\n"
        "        # A pre-finalizer poll may cache shard `batch_*` intermediates.\n"
        "        # A partitioned run is not downloadable until the canonical\n"
        "        # merged output appears, so re-list instead of completing on\n"
        "        # cached shard files.\n"
    )
    _replace_fresh_or_legacy(
        path,
        fresh=legacy_existing,
        legacy=desired_existing,
        desired=desired_existing,
        marker='item.get("filename") == "merged_results.out.gz"',
    )
    legacy_filter = '        if not name.startswith("batch_"):\n            continue\n'
    desired_filter = (
        '        if name == "merged_results.out.gz":\n'
        "            files = []\n"
        "            seen = set()\n"
        '        elif requires_merged_result or not name.startswith("batch_") or any(\n'
        '            item.get("filename") == "merged_results.out.gz" for item in files\n'
        "        ):\n"
        "            continue\n"
    )
    _replace_fresh_or_legacy(
        path,
        fresh=legacy_filter,
        legacy=desired_filter,
        desired=desired_filter,
        marker='if name == "merged_results.out.gz":',
    )


def _patch_canonical_merged_result_validation(path: Path) -> None:
    """Permit the exact canonical merged XML path without weakening traversal guards."""
    shard_only = (
        '    if not blob_path.split("/")[-1].startswith("batch_"):\n'
        '        raise HTTPException(400, "Invalid result blob path")\n'
    )
    canonical_or_shard = (
        '    basename = blob_path.split("/")[-1]\n'
        '    if not (basename.startswith("batch_") or basename == "merged_results.out.gz"):\n'
        '        raise HTTPException(400, "Invalid result blob path")\n'
    )
    _replace_fresh_or_legacy(
        path,
        fresh=shard_only,
        legacy=canonical_or_shard,
        desired=canonical_or_shard,
        marker='basename == "merged_results.out.gz"',
    )


def _patch_active_database_metadata(path: Path) -> None:
    """Project the same active-generation counts used by precise execution."""
    anchor = '    response.headers["X-Cache"] = cache_status\n    return meta\n'
    desired = (
        '    if safe == "core_nt":\n'
        "        try:\n"
        "            active_database = _exact_oracle.read_active_database(\n"
        "                blob_base=_blob_base(),\n"
        '                db_name="core_nt",\n'
        "                token=_storage_oauth_token(),\n"
        "            )\n"
        "        except Exception as exc:\n"
        "            raise HTTPException(\n"
        "                503,\n"
        '                "Active core_nt generation metadata is unavailable",\n'
        "            ) from exc\n"
        "        meta = {\n"
        "            **meta,\n"
        '            "snapshot": active_database.source_version,\n'
        '            "number_of_sequences": active_database.total_sequences,\n'
        '            "number_of_letters": active_database.total_letters,\n'
        "        }\n" + anchor
    )
    _replace_once_unless_marker(
        path,
        anchor,
        desired,
        '"snapshot": active_database.source_version',
    )


def _patch_result_readiness_payloads(path: Path) -> None:
    """Expose canonical result readiness on both public status surfaces."""
    external_legacy = (
        '    elif public_status == "success":\n'
        '        payload["completed_at"] = job_info.get("completed_at") or job_info.get("updated_at", "")\n'
        "        files = _list_result_files(job_info)\n"
        '        result_payload: dict[str, Any] = {"files": files}\n'
        '        if "hit_count" in job_info:\n'
        '            result_payload["hit_count"] = int(job_info.get("hit_count", 0) or 0)\n'
        '        payload["result"] = result_payload\n'
    )
    external_desired = (
        '    elif public_status == "success":\n'
        '        ready_at = job_info.get("completed_at") or job_info.get("updated_at", "")\n'
        '        payload["completed_at"] = ready_at\n'
        "        files = _list_result_files(job_info)\n"
        '        result_payload: dict[str, Any] = {"files": files}\n'
        '        if "hit_count" in job_info:\n'
        '            result_payload["hit_count"] = int(job_info.get("hit_count", 0) or 0)\n'
        '        payload["result"] = result_payload\n'
        '        payload["results_ready"] = bool(files)\n'
        "        if files:\n"
        '            payload["results_ready_at"] = ready_at\n'
        '            exact_oracle = job_info.get("exact_oracle")\n'
        "            try:\n"
        "                result_partitions = int(\n"
        '                    job_info.get("db_partitions")\n'
        '                    or exact_oracle.get("db_partitions", 0)\n'
        "                    if isinstance(exact_oracle, dict)\n"
        '                    else job_info.get("db_partitions", 0)\n'
        "                )\n"
        "            except (TypeError, ValueError):\n"
        "                result_partitions = 0\n"
        "            if result_partitions > 1:\n"
        '                payload["merged_at"] = ready_at\n'
    )
    _replace_once_unless_marker(
        path,
        external_legacy,
        external_desired,
        'payload["results_ready_at"] = ready_at',
    )

    status_anchor = '        "kubernetes": {"summary": job_info.get("k8s_summary", {})},\n    }\n'
    status_insertion = (
        '    if job_info.get("status") == "completed":\n'
        "        status_files = _list_result_files(job_info)\n"
        '        _status_payload["results_ready"] = bool(status_files)\n'
        "        if status_files:\n"
        '            ready_at = job_info.get("completed_at") or job_info.get("updated_at", "")\n'
        '            _status_payload["results_ready_at"] = ready_at\n'
        '            exact_oracle = job_info.get("exact_oracle")\n'
        "            try:\n"
        "                result_partitions = int(\n"
        '                    job_info.get("db_partitions")\n'
        '                    or exact_oracle.get("db_partitions", 0)\n'
        "                    if isinstance(exact_oracle, dict)\n"
        '                    else job_info.get("db_partitions", 0)\n'
        "                )\n"
        "            except (TypeError, ValueError):\n"
        "                result_partitions = 0\n"
        "            if result_partitions > 1:\n"
        '                _status_payload["merged_at"] = ready_at\n'
    )
    _insert_once(
        path,
        status_anchor,
        status_insertion,
        '_status_payload["results_ready_at"] = ready_at',
    )


def _patch_partitioned_completion_fail_closed(path: Path) -> None:
    """Never complete a partitioned job without its marker and canonical merge."""
    visibility_constant = (
        "RESULTS_VISIBILITY_GRACE_SECONDS = max(0, int(os.environ.get("
        '"ELB_OPENAPI_RESULTS_VISIBILITY_GRACE_SECONDS", "120")))\n'
    )
    _insert_once(
        path,
        visibility_constant,
        (
            "PARTITIONED_RESULT_FINALIZER_DEADLINE_SECONDS = max(\n"
            "    1,\n"
            "    int(os.environ.get(\n"
            '        "ELB_FINALIZER_ACTIVE_DEADLINE_SECONDS", "1800"\n'
            "    )),\n"
            ")\n"
        ),
        "PARTITIONED_RESULT_FINALIZER_DEADLINE_SECONDS = max(",
    )
    terminal_guard = (
        '    if job.get("status") in _TERMINAL_STATES or job.get("status") == "queued":\n'
        "        return job\n"
    )
    _insert_once(
        path,
        terminal_guard,
        "\n    requires_canonical_merge = _result_partition_count(job) > 1\n",
        "requires_canonical_merge = _result_partition_count(job) > 1",
    )
    legacy_visibility_fallback = (
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
    )
    fail_closed_visibility_fallback = (
        "        marker_age = _age_seconds(seen_at)\n"
        "        if (\n"
        "            requires_canonical_merge\n"
        "            and marker_age > PARTITIONED_RESULT_FINALIZER_DEADLINE_SECONDS\n"
        "        ):\n"
        "            updates = {\n"
        '                "status": "failed",\n'
        '                "phase": "finalizer_failed",\n'
        '                "error": "canonical merged result was not published before the finalizer deadline",\n'
        '                "last_progress_at": _now_iso(),\n'
        "            }\n"
        "            summary_snapshot = _snapshot_k8s_summary_for_terminal(job, elb_job_id)\n"
        "            if summary_snapshot is not None:\n"
        '                updates["k8s_summary"] = summary_snapshot\n'
        "            result = _update_job(job_id, **updates)\n"
        "            _notify_terminal_transition(job_id, updates)\n"
        "            return result\n"
        "        if (\n"
        "            not requires_canonical_merge\n"
        "            and marker_age > RESULTS_VISIBILITY_GRACE_SECONDS\n"
        "        ):\n"
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
    )
    _replace_once_unless_marker(
        path,
        legacy_visibility_fallback,
        fail_closed_visibility_fallback,
        "marker_age > PARTITIONED_RESULT_FINALIZER_DEADLINE_SECONDS",
    )
    summary_completion = (
        '        if summary.get("succeeded", 0) >= summary.get("total", 0) and summary.get("total", 0) > 0:\n'
        "            if _list_result_files(job):\n"
        '                updates.update({"status": "completed", "phase": "completed", "completed_at": _now_iso()})\n'
        "            else:\n"
        '                updates.update({"status": "running", "phase": "finalizing"})\n'
    )
    summary_fail_closed = (
        '        if summary.get("succeeded", 0) >= summary.get("total", 0) and summary.get("total", 0) > 0:\n'
        "            if requires_canonical_merge:\n"
        '                updates.update({"status": "running", "phase": "finalizing"})\n'
        "            elif _list_result_files(job):\n"
        '                updates.update({"status": "completed", "phase": "completed", "completed_at": _now_iso()})\n'
        "            else:\n"
        '                updates.update({"status": "running", "phase": "finalizing"})\n'
    )
    _replace_once_unless_marker(
        path,
        summary_completion,
        summary_fail_closed,
        "            if requires_canonical_merge:\n",
    )
    no_summary_completion = (
        "    else:\n"
        "        if _list_result_files(job):\n"
        '            updates.update({"status": "completed", "phase": "completed", "completed_at": _now_iso()})\n'
        "        else:\n"
        '            updates.update({"phase": "submitting"})\n'
    )
    no_summary_fail_closed = (
        "    else:\n"
        "        if not requires_canonical_merge and _list_result_files(job):\n"
        '            updates.update({"status": "completed", "phase": "completed", "completed_at": _now_iso()})\n'
        "        else:\n"
        '            updates.update({"phase": "submitting"})\n'
    )
    _replace_once_unless_marker(
        path,
        no_summary_completion,
        no_summary_fail_closed,
        "if not requires_canonical_merge and _list_result_files(job):",
    )


def _normalize_status_payload_tail(path: Path) -> None:
    """Collapse the inherited unreachable duplicate status payload tail."""
    eta_block = (
        '    if _eta is not None and _eta.enabled() and job_info.get("status") in {"queued", "dispatching", "submitting", "running"}:\n'
        "        with _jobs_lock:\n"
        "            _eta_jobs = [dict(v) for v in _jobs.values()]\n"
        "        _eta_out = _eta.compute_eta(job_info, _eta_jobs, MAX_ACTIVE_SUBMISSIONS)\n"
        "        if _eta_out:\n"
        '            _status_payload["eta"] = _eta_out\n'
    )
    passthrough_block = (
        '    _pt = job_info.get("passthrough")\n'
        "    if isinstance(_pt, dict) and _pt:\n"
        '        _status_payload["passthrough"] = _pt\n'
    )
    inherited = (
        eta_block
        + "    return _status_payload\n"
        + passthrough_block
        + eta_block
        + "    return _status_payload\n"
    )
    desired = (
        "    # Keep status metadata reachable before the single return.\n"
        + passthrough_block
        + eta_block
        + "    return _status_payload\n"
    )
    _replace_once_unless_marker(
        path,
        inherited,
        desired,
        "Keep status metadata reachable before the single return",
    )


def _patch_result_selection_policy(path: Path) -> None:
    """Carry an explicit native-vs-diversity selection policy to finalization."""
    _insert_once(
        path,
        "    is_b = req.query_fasta is not None\n",
        (
            "    web_blast_context = (\n"
            "        req.blast_options.web_blast_statistical_context.model_dump()\n"
            "        if (\n"
            "            req.blast_options is not None\n"
            "            and req.blast_options.web_blast_statistical_context is not None\n"
            "        )\n"
            '        else (req.model_extra or {}).get("web_blast_statistical_context")\n'
            "    )\n"
            "    selection_policy = (\n"
            "        req.blast_options.result_selection_policy\n"
            "        if req.blast_options is not None\n"
            '        else "native_top_n"\n'
            "    )\n"
            "    if (\n"
            '        selection_policy == "diversity_aware"\n'
            '        and web_blast_context not in (None, "")\n'
            "    ):\n"
            "        raise HTTPException(\n"
            "            400,\n"
            '            "web_blast_statistical_context requires native_top_n result selection",\n'
            "        )\n"
        ),
        "web_blast_statistical_context requires native_top_n result selection",
    )
    _insert_once(
        path,
        '    if req.batch_len is not None:\n        config["blast"]["batch-len"] = str(req.batch_len)\n',
        '    config["blast"]["result-selection-policy"] = selection_policy\n',
        'config["blast"]["result-selection-policy"]',
    )
    exact_legacy = (
        "    exact_oracle_info = None\n"
        '    if db_name == "core_nt" and profile in {"core_nt_precise", "precise", "core_nt_safe"}:\n'
        "        db_version = {\n"
    )
    exact_desired = (
        "    exact_oracle_info = None\n"
        "    if (\n"
        '        db_name == "core_nt"\n'
        '        and profile in {"core_nt_precise", "precise", "core_nt_safe"}\n'
        '        and selection_policy == "native_top_n"\n'
        "    ):\n"
        "        db_version = {\n"
    )
    _replace_fresh_or_legacy(
        path,
        fresh=exact_legacy,
        legacy=exact_desired,
        desired=exact_desired,
        marker='and selection_policy == "native_top_n"',
    )
    _insert_once(
        path,
        "    exact_oracle_info = None\n",
        (
            "    # Keep diversity-aware runs on the same active DB provenance.\n"
            '    if db_name == "core_nt" and profile in {"core_nt_precise", "precise", "core_nt_safe"}:\n'
            "        db_version = {\n"
            '            "version": active_database.source_version,\n'
            '            "source": "active_generation",\n'
            '            "detail": {\n'
            '                "number_of_letters": str(active_database.total_letters),\n'
            '                "number_of_sequences": str(active_database.total_sequences),\n'
            "            },\n"
            "        }\n"
        ),
        "Keep diversity-aware runs on the same active DB provenance",
    )
    _insert_once(
        path,
        '    if passthrough:\n        job_data["passthrough"] = passthrough\n',
        (
            '    job_data["result_selection_policy"] = selection_policy\n'
            '    job_data["db_partitions"] = int(\n'
            '        config["blast"].get("db-partitions", 0) or 0\n'
            "    )\n"
        ),
        'job_data["result_selection_policy"] = selection_policy',
    )
    _insert_once(
        path,
        '    if isinstance(_pt, dict) and _pt:\n        payload["passthrough"] = _pt\n',
        (
            '    payload["result_selection_policy"] = job_info.get(\n'
            '        "result_selection_policy", "native_top_n"\n'
            "    )\n"
            '    payload["db_partitions"] = int(job_info.get("db_partitions", 0) or 0)\n'
        ),
        'payload["result_selection_policy"] = job_info.get(',
    )
    _replace_once_unless_marker(
        path,
        '                context=(req.model_extra or {}).get("web_blast_statistical_context"),\n',
        "                context=web_blast_context,\n",
        "                context=web_blast_context,\n",
    )


def _patch_finalizer_failure_status(path: Path) -> None:
    """Make a terminal merge-finalizer failure visible and terminal."""
    _insert_once(
        path,
        '        "finalizer_active": 0,\n',
        '        "finalizer_failed_terminal": 0,\n',
        '"finalizer_failed_terminal": 0',
    )
    _replace_once_unless_marker(
        path,
        (
            '        elif app_label == "finalizer":\n'
            '            summary["finalizer_active"] += status.get("active", 0) or 0\n'
        ),
        (
            '        elif app_label == "finalizer":\n'
            '            summary["finalizer_active"] += status.get("active", 0) or 0\n'
            "            if job_failed_terminal:\n"
            '                summary["finalizer_failed_terminal"] += 1\n'
        ),
        'summary["finalizer_failed_terminal"] += 1',
    )
    _replace_once_unless_marker(
        path,
        '    if summary.get("submit_failed_terminal"):\n',
        (
            '    if summary.get("finalizer_failed_terminal"):\n'
            "        updates.update(\n"
            "            {\n"
            '                "status": "failed",\n'
            '                "phase": "finalizer_failed",\n'
            '                "error": "result merge finalizer failed or exceeded its deadline",\n'
            "            }\n"
            "        )\n"
            '    elif summary.get("submit_failed_terminal"):\n'
        ),
        '"phase": "finalizer_failed"',
    )


def _patch_terminal_webhook_runtime_id(path: Path) -> None:
    """Attach a genuine ElasticBLAST runtime id to terminal webhooks."""

    _insert_once(
        path,
        "            merged = {**job_snap, **updates}\n",
        (
            "            runtime_job_id = _effective_elb_job_id(merged)\n"
            '            if runtime_job_id.startswith("job-") and runtime_job_id != job_id:\n'
            '                payload["elb_job_id"] = runtime_job_id\n'
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
        '    if current.startswith("job-"):\n        return current\n',
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
            'job_info.get("elb_job_id") != effective_elb_job_id:',
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


def _patch_submit_runtime_id_priority(path: Path) -> None:
    """Ignore non-runtime correlation values before parsing submit output."""

    fresh = (
        "        _update_job(\n"
        "            job_id,\n"
        "            status=status,\n"
        '            phase="submitted" if status == "running" else status,\n'
        '            elb_job_id=payload.get("correlation_id") or job_id,\n'
    )
    legacy = (
        "        _update_job(\n"
        "            job_id,\n"
        "            status=status,\n"
        '            phase="submitted" if status == "running" else status,\n'
        "            elb_job_id=(\n"
        '                payload.get("correlation_id")\n'
        '                or _discover_elb_job_id_from_submit_output(job_id, result.stdout or "")\n'
        "                or job_id\n"
        "            ),\n"
    )
    desired = (
        '        correlation_id = str(payload.get("correlation_id") or "")\n'
        "        canonical_correlation_id = (\n"
        "            correlation_id.lower()\n"
        '            if re.fullmatch(r"job-[0-9a-f]{32}", correlation_id, re.IGNORECASE)\n'
        '            else ""\n'
        "        )\n"
        "        _update_job(\n"
        "            job_id,\n"
        "            status=status,\n"
        '            phase="submitted" if status == "running" else status,\n'
        "            elb_job_id=(\n"
        "                canonical_correlation_id\n"
        '                or _discover_elb_job_id_from_submit_output(job_id, result.stdout or "")\n'
        "                or job_id\n"
        "            ),\n"
    )
    _replace_fresh_or_legacy(
        path,
        fresh=fresh,
        legacy=legacy,
        desired=desired,
        marker="canonical_correlation_id = (",
    )


def _harden_elb_scripts_configmap_reconciliation(path: Path) -> None:
    """Reconcile installed ElasticBLAST scripts instead of trusting stale keys."""

    text = path.read_text()
    marker = "ELB scripts ConfigMap drift detected"
    if marker in text:
        return
    function_name = "def _ensure_elb_scripts_configmap() -> None:\n"
    if text.count(function_name) != 1:
        raise RuntimeError("expected exactly one ELB scripts ConfigMap helper")
    start = text.index(function_name)
    end = text.find("\n\ndef ", start + len(function_name))
    if end < 0:
        raise RuntimeError("could not isolate ELB scripts ConfigMap helper")
    replacement = """def _ensure_elb_scripts_configmap() -> None:
    required_scripts = {
        "blast-run-aks.sh",
        "elb-finalizer-aks.sh",
        "init-db-download-aks.sh",
        "init-db-shard-aks.sh",
        "query-download-ssd-aks.sh",
        "results-export-aks.sh",
    }
    scripts_dir = files("elastic_blast").joinpath("templates/scripts")
    scripts_path = Path(str(scripts_dir))
    desired_scripts = {
        script_path.name: script_path.read_text(encoding="utf-8")
        for script_path in scripts_path.iterdir()
        if script_path.is_file() and script_path.suffix == ".sh"
    }
    desired_size = sum(
        len(name.encode("utf-8")) + len(content.encode("utf-8"))
        for name, content in desired_scripts.items()
    )
    if desired_size > 900_000:
        raise RuntimeError(
            f"Installed ElasticBLAST scripts exceed ConfigMap limit: {desired_size} bytes"
        )
    missing = sorted(required_scripts.difference(desired_scripts))
    if missing:
        raise RuntimeError(f"Installed ElasticBLAST scripts are incomplete: {missing}")

    existing_data: dict[str, str] = {}
    try:
        existing = safe_exec(
            ["kubectl", "get", "configmap", "elb-scripts", "-o", "json"],
            timeout=10,
        )
        raw_data = json.loads(existing.stdout or "{}").get("data", {})
        if isinstance(raw_data, dict):
            existing_data = {
                str(name): str(content) for name, content in raw_data.items()
            }
    except Exception as exc:
        logger.info(
            "ELB scripts ConfigMap lookup unavailable; reconciling reason=%s",
            type(exc).__name__,
        )

    drifted = sorted(
        name
        for name, content in desired_scripts.items()
        if existing_data.get(name) != content
    )
    if not drifted:
        return
    logger.info(
        "ELB scripts ConfigMap drift detected; reconciling scripts=%s",
        ",".join(drifted),
    )
    dry_run = subprocess.run(
        [
            "kubectl",
            "create",
            "configmap",
            "elb-scripts",
            f"--from-file={scripts_path}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=dry_run.stdout,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    try:
        applied = safe_exec(
            ["kubectl", "get", "configmap", "elb-scripts", "-o", "json"],
            timeout=10,
        )
        applied_data = json.loads(applied.stdout or "{}").get("data", {})
    except Exception as exc:
        raise RuntimeError(
            "ELB scripts ConfigMap verification failed "
            f"reason={type(exc).__name__}"
        ) from None
    if not isinstance(applied_data, dict):
        raise RuntimeError("ELB scripts ConfigMap verification returned invalid data")
    remaining_drift = sorted(
        name
        for name, content in desired_scripts.items()
        if applied_data.get(name) != content
    )
    if remaining_drift:
        raise RuntimeError(
            "ELB scripts ConfigMap verification found drift scripts="
            + ",".join(remaining_drift)
        )"""
    path.write_text(text[:start] + replacement + text[end:])


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
        'elb-job-id: "${BLAST_ELB_JOB_ID}"': 3,
        "grep -q 'def _wait_for_elb_init_jobs('": 3,
        "grep -q 'ELB DB reader lock'": 3,
        "grep -q 'name: ELB_DB_READER_LOCK'": 3,
        "grep -q 'DISK_PREFLIGHT required_bytes='": 3,
        "grep -q 'ELB DB writer lock'": 3,
        "grep -q 'name: ELB_DB_WRITER_LOCK'": 3,
        "for template in blast-batch-job-local-ssd-aks.yaml.template": 3,
        "grep -Fq 'name: init-ssd-${BLAST_ELB_JOB_ID}-${NODE_ORDINAL}'": 3,
        "|| exit 1; done": 6,
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

    import ast

    text = path.read_text()
    required = (
        "import json\n",
        "import subprocess\n",
        "from importlib.resources import files",
        "from pathlib import Path",
        "from util import run_cancellable, safe_exec",
        "import exact_oracle as _exact_oracle",
        "import result_selection as _result_selection",
        'logger = logging.getLogger("elb-openapi")',
        'config["cluster"].pop("exp-skip-warmed-ssd-init", None)',
        '"init-db-shard-aks.sh",',
        'script_path.read_text(encoding="utf-8")',
        "desired_size > 900_000",
        "ELB scripts ConfigMap drift detected",
        "ELB scripts ConfigMap verification found drift",
        "def _discover_elb_job_id_from_submit_output(",
        "def _effective_elb_job_id(",
        'canonical_current = re.fullmatch(r"job-[0-9a-f]{32}"',
        "canonical_correlation_id = (",
        'payload["elb_job_id"] = runtime_job_id',
        'def _job_marker_phase(results_url: str, elb_job_id: str = "")',
        're.fullmatch(r"job-[0-9a-f]{32}", elb_job_id, re.IGNORECASE)',
        'safe_exec(["kubectl", "get", "jobs", "-l", f"elb-job-id={elb_job_id}"',
        'safe_exec(["kubectl", "get", "pods", "-l", f"elb-job-id={elb_job_id}"',
        "def _result_partition_count(job_info):",
        "PARTITIONED_RESULT_FINALIZER_DEADLINE_SECONDS = max(",
        "requires_canonical_merge = _result_partition_count(job) > 1",
        "marker_age > PARTITIONED_RESULT_FINALIZER_DEADLINE_SECONDS",
        "not requires_canonical_merge",
        "canonical merged result was not published before the finalizer deadline",
        "import reference_context as _reference_context",
        'def resolve_web_blast_statistical_context(',
        "reference context active database metadata unavailable error_type=%s",
        "response_model=WebBlastStatisticalContextResponse",
        "response_model=JobListResponse",
        "response_model=JobStatusResponse",
        "opts = _exact_oracle.ensure_tabular_raw_score(opts)",
        "_result_selection.prepare_sequence_diversity_options(",
        'config["blast"]["requested-max-target-seqs"] = str(',
        '"sequence_identity_mode": _result_selection.SEQUENCE_IDENTITY_MODE',
        "active_database = _exact_oracle.read_active_database(",
        "opts = _exact_oracle.preserve_or_set_search_space(",
        "opts, active_database.search_space",
        "opts, web_blast_statistics = _exact_oracle.prepare_web_blast_statistics(",
        "_exact_oracle.attach_web_blast_statistics(",
        "active_database.db_prefix",
        "active_database.shard_layout_prefix",
        "exact_oracle_info = _exact_oracle.attach_db_order_oracle(",
        "expected_source_version=active_database.source_version",
        '"source": "active_generation"',
        '"db_prefix": active_database.db_prefix',
        'job_data["exact_oracle"] = exact_oracle_info',
        'job_data["web_blast_statistics"] = web_blast_statistics.as_dict()',
        'for _runtime_key in ("exact_oracle", "web_blast_statistics")',
        '"-soft_masking true" if opts.soft_masking',
        "req.options.db_effective_search_space is not None",
        "req.options.web_blast_statistical_context is not None",
    )
    missing = [fragment for fragment in required if fragment not in text]
    forbidden = (
        "required_scripts.issubset(set(data))",
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
            f"OpenAPI app runtime policy mismatch: missing={missing}, forbidden={present}"
        )

    tree = ast.parse(text)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    expected_call_counts = {
        "read_active_database": 3,
        "preserve_or_set_search_space": 1,
        "prepare_web_blast_statistics": 1,
        "attach_web_blast_statistics": 1,
        "attach_db_order_oracle": 1,
    }
    for call_name, expected_count in expected_call_counts.items():
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == call_name
        ]
        if len(calls) != expected_count:
            raise RuntimeError(
                "OpenAPI runtime policy requires "
                f"{expected_count} {call_name} call(s), found {len(calls)}"
            )
        for call in calls:
            current: ast.AST | None = call
            while current in parents:
                current = parents[current]
                if isinstance(current, (ast.For, ast.AsyncFor, ast.While)):
                    raise RuntimeError(f"OpenAPI {call_name} call must not run inside a loop")


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
        "    python3 /tmp/patch_elastic_blast.py /tmp/elb-src /tmp/merge-sharded-results.sh && \\\n"
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
    source_hardening_check_legacy = (
        "    grep -q 'def _wait_for_elb_init_jobs(' "
        "/tmp/elb-src/src/elastic_blast/kubernetes.py && \\\n"
        "    grep -q 'ELB DB reader lock' "
        "/tmp/elb-src/src/elastic_blast/templates/scripts/blast-run-aks.sh && \\\n"
        "    grep -q 'name: ELB_DB_READER_LOCK' "
        "/tmp/elb-src/src/elastic_blast/templates/"
        "blast-batch-job-shard-ssd-aks.yaml.template && \\\n"
        "    grep -Fq 'name: init-ssd-${BLAST_ELB_JOB_ID}-${NODE_ORDINAL}' "
        "/tmp/elb-src/src/elastic_blast/templates/"
        "job-init-ssd-shard-aks.yaml.template && \\\n"
    )
    source_hardening_check = (
        "    grep -q 'def _wait_for_elb_init_jobs(' "
        "/tmp/elb-src/src/elastic_blast/kubernetes.py && \\\n"
        "    grep -q 'ELB DB reader lock' "
        "/tmp/elb-src/src/elastic_blast/templates/scripts/blast-run-aks.sh && \\\n"
        "    for template in blast-batch-job-local-ssd-aks.yaml.template "
        "blast-batch-job-shard-ssd-aks.yaml.template; do "
        "grep -q 'name: ELB_DB_READER_LOCK' "
        '"/tmp/elb-src/src/elastic_blast/templates/${template}" || exit 1; done && \\\n'
        "    grep -q 'ELB DB writer lock' "
        "/tmp/elb-src/src/elastic_blast/templates/scripts/init-db-download-aks.sh && \\\n"
        "    grep -q 'name: ELB_DB_WRITER_LOCK' "
        "/tmp/elb-src/src/elastic_blast/templates/"
        "job-init-local-ssd-aks.yaml.template && \\\n"
        "    grep -q 'DISK_PREFLIGHT required_bytes=' "
        "/tmp/elb-src/src/elastic_blast/templates/scripts/init-db-shard-aks.sh && \\\n"
        "    grep -Fq 'name: init-ssd-${BLAST_ELB_JOB_ID}-${NODE_ORDINAL}' "
        "/tmp/elb-src/src/elastic_blast/templates/"
        "job-init-ssd-shard-aks.yaml.template && \\\n"
    )
    _replace_fresh_or_legacy(
        path,
        fresh=source_identity_check,
        legacy=source_identity_check + source_hardening_check_legacy,
        desired=source_identity_check + source_hardening_check,
        marker=source_hardening_check.strip(),
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
    system_hardening_check_legacy = (
        "    grep -q 'def _wait_for_elb_init_jobs(' "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/kubernetes.py && \\\n"
        "    grep -q 'ELB DB reader lock' "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/"
        "scripts/blast-run-aks.sh && \\\n"
        "    grep -q 'name: ELB_DB_READER_LOCK' "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/"
        "blast-batch-job-shard-ssd-aks.yaml.template && \\\n"
        "    grep -Fq 'name: init-ssd-${BLAST_ELB_JOB_ID}-${NODE_ORDINAL}' "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/"
        "job-init-ssd-shard-aks.yaml.template && \\\n"
    )
    system_hardening_check = (
        "    grep -q 'def _wait_for_elb_init_jobs(' "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/kubernetes.py && \\\n"
        "    grep -q 'ELB DB reader lock' "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/"
        "scripts/blast-run-aks.sh && \\\n"
        "    for template in blast-batch-job-local-ssd-aks.yaml.template "
        "blast-batch-job-shard-ssd-aks.yaml.template; do "
        "grep -q 'name: ELB_DB_READER_LOCK' "
        '"/usr/local/lib/python3.11/site-packages/elastic_blast/templates/${template}" '
        "|| exit 1; done && \\\n"
        "    grep -q 'ELB DB writer lock' "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/"
        "scripts/init-db-download-aks.sh && \\\n"
        "    grep -q 'name: ELB_DB_WRITER_LOCK' "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/"
        "job-init-local-ssd-aks.yaml.template && \\\n"
        "    grep -q 'DISK_PREFLIGHT required_bytes=' "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/"
        "scripts/init-db-shard-aks.sh && \\\n"
        "    grep -Fq 'name: init-ssd-${BLAST_ELB_JOB_ID}-${NODE_ORDINAL}' "
        "/usr/local/lib/python3.11/site-packages/elastic_blast/templates/"
        "job-init-ssd-shard-aks.yaml.template && \\\n"
    )
    _replace_fresh_or_legacy(
        path,
        fresh=system_identity_check,
        legacy=system_identity_check + system_hardening_check_legacy,
        desired=system_identity_check + system_hardening_check,
        marker=system_hardening_check.strip(),
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
    venv_hardening_check_legacy = (
        "    && grep -q 'def _wait_for_elb_init_jobs(' "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/kubernetes.py \\\n"
        "    && grep -q 'ELB DB reader lock' "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/"
        "scripts/blast-run-aks.sh \\\n"
        "    && grep -q 'name: ELB_DB_READER_LOCK' "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/"
        "blast-batch-job-shard-ssd-aks.yaml.template \\\n"
        "    && grep -Fq 'name: init-ssd-${BLAST_ELB_JOB_ID}-${NODE_ORDINAL}' "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/"
        "job-init-ssd-shard-aks.yaml.template \\\n"
    )
    venv_hardening_check = (
        "    && grep -q 'def _wait_for_elb_init_jobs(' "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/kubernetes.py \\\n"
        "    && grep -q 'ELB DB reader lock' "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/"
        "scripts/blast-run-aks.sh \\\n"
        "    && for template in blast-batch-job-local-ssd-aks.yaml.template "
        "blast-batch-job-shard-ssd-aks.yaml.template; do "
        "grep -q 'name: ELB_DB_READER_LOCK' "
        '"/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/${template}" '
        "|| exit 1; done \\\n"
        "    && grep -q 'ELB DB writer lock' "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/"
        "scripts/init-db-download-aks.sh \\\n"
        "    && grep -q 'name: ELB_DB_WRITER_LOCK' "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/"
        "job-init-local-ssd-aks.yaml.template \\\n"
        "    && grep -q 'DISK_PREFLIGHT required_bytes=' "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/"
        "scripts/init-db-shard-aks.sh \\\n"
        "    && grep -Fq 'name: init-ssd-${BLAST_ELB_JOB_ID}-${NODE_ORDINAL}' "
        "/opt/venv/lib/python3.11/site-packages/elastic_blast/templates/"
        "job-init-ssd-shard-aks.yaml.template \\\n"
    )
    _replace_fresh_or_legacy(
        path,
        fresh=venv_identity_check,
        legacy=venv_identity_check + venv_hardening_check_legacy,
        desired=venv_identity_check + venv_hardening_check,
        marker=venv_hardening_check.strip(),
    )
    _validate_dockerfile_runtime_policy(path)


def patch_app(root: Path) -> None:
    _copy_app_overlay(root)
    _ensure_reference_context_dependency(root)
    _patch_external_soft_masking(root)
    _patch_openapi_response_schemas(root)
    _patch_reference_context_endpoint(root)
    path = root / "app" / "main.py"
    _patch_canonical_merged_result_validation(root / "app" / "helpers.py")
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
    _harden_elb_scripts_configmap_reconciliation(path)
    _insert_once(
        path,
        "from util import run_cancellable, safe_exec\n",
        (
            "\ntry:\n"
            "    import exact_oracle as _exact_oracle\n"
            "except Exception:  # pragma: no cover - validated before precise submit\n"
            "    _exact_oracle = None\n"
        ),
        "import exact_oracle as _exact_oracle",
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
            "    web_blast_statistics = None\n"
            '    if db_name == "core_nt" and profile in {"core_nt_precise", "precise", "core_nt_safe"}:\n'
            "        if _exact_oracle is None:\n"
            '            raise HTTPException(503, "Exact DB-order oracle support is unavailable")\n'
            "        opts = _exact_oracle.ensure_tabular_raw_score(opts)\n"
            '        config["blast"]["options"] = opts\n'
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
        '    if db_name == "core_nt" and profile in {"core_nt_precise", "precise", "core_nt_safe"}:\n',
        (
            "        if _exact_oracle is None:\n"
            '            raise HTTPException(503, "Exact DB-order oracle support is unavailable")\n'
            "        opts = _exact_oracle.ensure_tabular_raw_score(opts)\n"
            '        config["blast"]["options"] = opts\n'
        ),
        "opts = _exact_oracle.ensure_tabular_raw_score(opts)",
    )
    _insert_once(
        path,
        '    profile = str(req.resource_profile or "").strip().lower()\n',
        "    web_blast_statistics = None\n",
        "    web_blast_statistics = None\n",
    )
    _replace_stale_core_nt_search_space_fallback(path)
    _patch_canonical_merged_result_discovery(path)
    _patch_active_database_metadata(path)
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
    _patch_submit_runtime_id_priority(path)
    _insert_once(
        path,
        (
            "    blast_version = _blast_version_detail()\n"
            "    db_version = _db_version_detail(db_name)\n"
        ),
        (
            "    exact_oracle_info = None\n"
            '    if db_name == "core_nt" and profile in {"core_nt_precise", "precise", "core_nt_safe"}:\n'
            "        db_version = {\n"
            '            "version": active_database.source_version,\n'
            '            "source": "active_generation",\n'
            '            "detail": {\n'
            '                "number_of_letters": str(active_database.total_letters),\n'
            '                "number_of_sequences": str(active_database.total_sequences),\n'
            '                "db_prefix": active_database.db_prefix,\n'
            '                "shard_layout_prefix": active_database.shard_layout_prefix,\n'
            "            },\n"
            "        }\n"
            "        try:\n"
            "            exact_oracle_info = _exact_oracle.attach_db_order_oracle(\n"
            "                blob_base=_blob_base(),\n"
            "                results_url=results_url,\n"
            "                db_name=db_name,\n"
            "                expected_source_version=active_database.source_version,\n"
            "                token=_storage_oauth_token(),\n"
            "            ).as_dict()\n"
            "            exact_oracle_info.update(\n"
            "                {\n"
            '                    "candidate_selection": (\n'
            '                        "monolithic_full_database"\n'
            "                        if partitions == 1\n"
            '                        else "partitioned_shards"\n'
            "                    ),\n"
            '                    "db_partitions": partitions,\n'
            '                    "memory_mode": (\n'
            '                        "disk_backed_bounded"\n'
            "                        if web_blast_statistics is not None and partitions == 1\n"
            '                        else "memory_cached_shards"\n'
            "                    ),\n"
            '                    "memory_request": "104Gi" if partitions == 1 else None,\n'
            '                    "memory_limit": "112Gi" if partitions == 1 else None,\n'
            "                }\n"
            "            )\n"
            "            if one_shard_layout is not None:\n"
            "                exact_oracle_info.update(one_shard_layout.as_dict())\n"
            "            if web_blast_statistics is not None:\n"
            "                exact_oracle_info.update(\n"
            "                    _exact_oracle.validate_web_blast_execution_options(\n"
            "                        opts, program=req.program\n"
            "                    )\n"
            "                )\n"
            "            if web_blast_statistics is not None:\n"
            "                _exact_oracle.attach_web_blast_statistics(\n"
            "                    blob_base=_blob_base(),\n"
            "                    results_url=results_url,\n"
            "                    statistics=web_blast_statistics,\n"
            "                    token=_storage_oauth_token(),\n"
            "                )\n"
            "        except Exception as exc:\n"
            "            logger.warning(\n"
            '                "exact DB-order oracle attach failed job=%s db=%s reason=%s",\n'
            "                job_id,\n"
            "                db_name,\n"
            "                type(exc).__name__,\n"
            "            )\n"
            "            raise HTTPException(\n"
            "                503,\n"
            '                "A ready same-generation DB-order oracle is required for exact sharded results",\n'
            "            ) from exc\n"
        ),
        "exact_oracle_info = None",
    )
    _patch_web_blast_candidate_selection_evidence(path)
    _insert_once(
        path,
        '    if passthrough:\n        job_data["passthrough"] = passthrough\n',
        (
            "    if exact_oracle_info is not None:\n"
            '        job_data["exact_oracle"] = exact_oracle_info\n'
            "    if web_blast_statistics is not None:\n"
            '        job_data["web_blast_statistics"] = web_blast_statistics.as_dict()\n'
        ),
        'job_data["exact_oracle"] = exact_oracle_info',
    )
    _insert_once(
        path,
        '    if isinstance(_pt, dict) and _pt:\n        payload["passthrough"] = _pt\n',
        (
            '    for _runtime_key in ("exact_oracle", "web_blast_statistics"):\n'
            "        _runtime_value = job_info.get(_runtime_key)\n"
            "        if isinstance(_runtime_value, dict) and _runtime_value:\n"
            "            payload[_runtime_key] = _runtime_value\n"
        ),
        'for _runtime_key in ("exact_oracle", "web_blast_statistics")',
    )
    _patch_result_selection_policy(path)
    _patch_finalizer_failure_status(path)
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
    marker_fresh = '    marker = _job_marker_phase(job.get("results", ""))\n'
    marker_legacy = (
        "    elb_job_id = _effective_elb_job_id(job)\n"
        '    marker = _job_marker_phase(job.get("results", ""), elb_job_id)\n'
    )
    marker_desired = (
        "    elb_job_id = _effective_elb_job_id(job)\n"
        '    marker_results_url = str(job.get("results", "")).rstrip("/")\n'
        "    marker = None\n"
        '    if marker_results_url and re.fullmatch(r"job-[0-9a-f]{32}", elb_job_id, re.IGNORECASE):\n'
        '        marker = _job_marker_phase(f"{marker_results_url}/{elb_job_id}")\n'
        "    if marker is None:\n"
        "        marker = _job_marker_phase(marker_results_url)\n"
    )
    _replace_fresh_or_legacy(
        path,
        fresh=marker_fresh,
        legacy=marker_legacy,
        desired=marker_desired,
        marker='    marker_results_url = str(job.get("results", "")).rstrip("/")\n',
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
    )  # Primary polling endpoint GET /v1/jobs/{id}/status (get_job_status) builds
    # its own inline dict and does NOT route through _external_job_payload, so
    # the ETA hook above never reaches it. Inject the same gated projection here
    # so callers polling the canonical status_url see `eta` for active/queued
    # jobs. Terminal jobs are skipped (compute_eta returns None anyway).
    _replace_once_unless_marker(
        path,
        "    return {\n"
        '        "job_id": job_id,\n'
        '        "status": job_info.get("status", "unknown"),\n',
        "    _status_payload: dict[str, Any] = {\n"
        '        "job_id": job_id,\n'
        '        "status": job_info.get("status", "unknown"),\n',
        "    _status_payload: dict[str, Any] = {\n",
    )
    _replace_once_unless_marker(
        path,
        '        "kubernetes": {"summary": job_info.get("k8s_summary", {})},\n    }\n',
        '        "kubernetes": {"summary": job_info.get("k8s_summary", {})},\n'
        "    }\n"
        '    if _eta is not None and _eta.enabled() and job_info.get("status") in {"queued", "dispatching", "submitting", "running"}:\n'
        "        with _jobs_lock:\n"
        "            _eta_jobs = [dict(v) for v in _jobs.values()]\n"
        "        _eta_out = _eta.compute_eta(job_info, _eta_jobs, MAX_ACTIVE_SUBMISSIONS)\n"
        "        if _eta_out:\n"
        '            _status_payload["eta"] = _eta_out\n'
        "    return _status_payload\n",
        '            _status_payload["eta"] = _eta_out\n    return _status_payload\n',
    )
    _normalize_status_payload_tail(path)
    _patch_partitioned_completion_fail_closed(path)
    _patch_result_readiness_payloads(path)
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
    _validate_copied_runtime_policy(root)
    print("patched docker-openapi build context for dashboard OpenAPI runtime policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
