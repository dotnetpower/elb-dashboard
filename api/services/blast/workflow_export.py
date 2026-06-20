"""Workflow-manager export for a submitted BLAST job (roadmap R3, issue #57).

Responsibility: render a self-contained Nextflow / Snakemake / CWL / WDL module
that re-submits a BLAST job through ``POST /api/blast/jobs`` with the *exact
parameter set* of a source job, taking the query FASTA as the only runtime input
so a researcher can drop the module into a pipeline. Pure string/dataclass
rendering from an already-loaded submit snapshot — no Azure or network calls.
Edit boundaries: keep this side-effect-free and dependency-light (stdlib only).
HTTP/auth/owner checks live in the route (`api/routes/blast/jobs_detail.py`);
the canonical submit snapshot is produced by
`api/services/blast/submit_payload.canonical_submit_snapshot`.
Key entry points: `SUPPORTED_WORKFLOW_FORMATS`, `build_pinned_request`,
`render_workflow_export`, `WorkflowExport`.
Risky contracts: NEVER pin ``idempotency_key`` / ``external_correlation_id`` —
that would collapse every pipeline run onto the source job's id. NEVER embed a
bearer token or storage URL in the rendered file; the runtime reads
``ELB_TOKEN`` / ``ELB_BASE_URL`` from the environment. Pinned values come only
from our own canonical snapshot (db charset is ``[A-Za-z0-9._/-]``, program is an
enum, options are numeric/bool) so they are JSON-escaped via ``json.dumps`` and
cannot break out of the embedded heredoc.
Validation: `uv run pytest -q api/tests/test_blast_workflow_export.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

SUPPORTED_WORKFLOW_FORMATS: tuple[str, ...] = ("nextflow", "snakemake", "cwl", "wdl")

_FORMAT_FILENAMES: dict[str, str] = {
    "nextflow": "main.nf",
    "snakemake": "Snakefile",
    "cwl": "blast_submit.cwl",
    "wdl": "blast_submit.wdl",
}

# CWL/WDL are YAML/HCL-ish; serve as text so a browser/CLI downloads them as-is.
_MEDIA_TYPE = "text/plain; charset=utf-8"


@dataclass(frozen=True)
class WorkflowExport:
    """A rendered, downloadable workflow module."""

    format: str
    filename: str
    media_type: str
    content: str


class UnsupportedFormatError(ValueError):
    """Raised when an unknown workflow format is requested."""


class MissingDatabaseError(ValueError):
    """Raised when the source job has no recorded database to pin."""


def _pinned_options(snapshot_options: dict | None) -> dict:
    """Map a canonical options dict onto the ExternalBlastOptions subset.

    Robust to key drift: only fields actually present in the source snapshot are
    emitted, so the submit endpoint's documented defaults fill the rest.
    """
    o = snapshot_options if isinstance(snapshot_options, dict) else {}
    out: dict[str, object] = {}

    for key in ("evalue", "expect", "expect_value"):
        if o.get(key) not in (None, ""):
            try:
                out["evalue"] = float(o[key])
            except (TypeError, ValueError):
                pass
            break

    if o.get("word_size") not in (None, ""):
        try:
            out["word_size"] = int(o["word_size"])
        except (TypeError, ValueError):
            pass

    for key in ("max_target_seqs", "hitlist_size"):
        if o.get(key) not in (None, ""):
            try:
                out["max_target_seqs"] = int(o[key])
            except (TypeError, ValueError):
                pass
            break

    if o.get("low_complexity_filter") is not None:
        out["dust"] = bool(o["low_complexity_filter"])
    elif o.get("dust") is not None:
        out["dust"] = bool(o["dust"])

    mode = o.get("sharding_mode")
    if mode in ("off", "approximate", "precise"):
        out["sharding_mode"] = mode

    for key in ("db_effective_search_space", "searchsp", "search_space"):
        if o.get(key) not in (None, ""):
            try:
                out["db_effective_search_space"] = int(o[key])
            except (TypeError, ValueError):
                pass
            break

    return out


def build_pinned_request(snapshot: dict) -> dict:
    """Return the pinned submit body (no ``query_fasta``) from a job snapshot.

    The query FASTA is intentionally omitted — the rendered workflow injects it
    at runtime from a pipeline input. ``idempotency_key`` /
    ``external_correlation_id`` are intentionally NOT pinned so each pipeline run
    is a fresh job.
    """
    program = str(snapshot.get("program") or "blastn").strip() or "blastn"
    database = str(snapshot.get("database") or snapshot.get("db") or "").strip()
    if not database:
        raise MissingDatabaseError("source job has no recorded database to export")

    body: dict[str, object] = {"program": program, "db": database}

    options = _pinned_options(snapshot.get("options"))
    if options:
        body["options"] = options

    # Taxonomy exclusion/inclusion, best-effort from snapshot or its options.
    src_options = snapshot.get("options") if isinstance(snapshot.get("options"), dict) else {}
    taxid = snapshot.get("taxid")
    if taxid in (None, ""):
        taxid = src_options.get("taxid")
    try:
        taxid_int = int(taxid) if taxid not in (None, "") else None
    except (TypeError, ValueError):
        taxid_int = None
    if taxid_int and taxid_int >= 1:
        body["taxid"] = taxid_int
        is_inclusive = snapshot.get("is_inclusive")
        if is_inclusive is None:
            is_inclusive = src_options.get("is_inclusive")
        if isinstance(is_inclusive, bool):
            body["is_inclusive"] = is_inclusive

    profile = snapshot.get("resource_profile")
    if isinstance(profile, str) and profile.strip():
        body["resource_profile"] = profile.strip()

    return body


def _submit_script(pinned: dict) -> str:
    """Return the stdlib-only python submit snippet shared by every format.

    Reads ``ELB_BASE_URL`` / ``ELB_TOKEN`` from the environment and the query
    FASTA path from ``ELB_QUERY_FASTA`` (default ``query.fasta``); merges the
    query into the pinned body and POSTs to ``/api/blast/jobs``.
    """
    pinned_json = json.dumps(pinned, sort_keys=True)
    return (
        "import json, os, sys, urllib.request\n"
        f"body = json.loads(r'''{pinned_json}''')\n"
        'query_path = os.environ.get("ELB_QUERY_FASTA", "query.fasta")\n'
        'with open(query_path, "r", encoding="utf-8") as fh:\n'
        '    body["query_fasta"] = fh.read()\n'
        'base = os.environ["ELB_BASE_URL"].rstrip("/")\n'
        'token = os.environ["ELB_TOKEN"]\n'
        "req = urllib.request.Request(\n"
        '    base + "/api/blast/jobs",\n'
        "    data=json.dumps(body).encode(),\n"
        "    headers={\n"
        '        "Authorization": "Bearer " + token,\n'
        '        "Content-Type": "application/json",\n'
        "    },\n"
        '    method="POST",\n'
        ")\n"
        "with urllib.request.urlopen(req) as resp:\n"
        "    sys.stdout.write(resp.read().decode())\n"
    )


def _indent(text: str, prefix: str) -> str:
    return "".join(
        prefix + line if line.strip() else line
        for line in text.splitlines(keepends=True)
    )


def _render_nextflow(job_id: str, pinned: dict) -> str:
    script = _submit_script(pinned)
    return (
        "#!/usr/bin/env nextflow\n"
        "nextflow.enable.dsl=2\n\n"
        f"// Generated by ElasticBLAST Control Plane from job {job_id}.\n"
        "// Re-submits a BLAST job with the source job's exact parameters.\n"
        "// Requires env: ELB_BASE_URL, ELB_TOKEN. Run: nextflow run main.nf --query my.fasta\n\n"
        "params.query = 'query.fasta'\n\n"
        "process blast_submit {\n"
        "    input:\n"
        "    path query_fasta\n\n"
        "    script:\n"
        '    """\n'
        "    ELB_QUERY_FASTA=${query_fasta} python3 <<'PYEOF'\n"
        f"{script}"
        "PYEOF\n"
        '    """\n'
        "}\n\n"
        "workflow {\n"
        "    blast_submit(Channel.fromPath(params.query))\n"
        "}\n"
    )


def _render_snakemake(job_id: str, pinned: dict) -> str:
    script = _submit_script(pinned)
    return (
        f"# Generated by ElasticBLAST Control Plane from job {job_id}.\n"
        "# Re-submits a BLAST job with the source job's exact parameters.\n"
        "# Requires env: ELB_BASE_URL, ELB_TOKEN. Run: snakemake -j1 --config query=my.fasta\n\n"
        'QUERY = config.get("query", "query.fasta")\n\n'
        "rule blast_submit:\n"
        "    input:\n"
        "        query=QUERY,\n"
        "    shell:\n"
        '        r"""\n'
        "        ELB_QUERY_FASTA={input.query} python3 <<'PYEOF'\n"
        f"{script}"
        "PYEOF\n"
        '        """\n'
    )


def _render_cwl(job_id: str, pinned: dict) -> str:
    script = _submit_script(pinned)
    embedded = _indent(script, "          ")
    return (
        "#!/usr/bin/env cwl-runner\n"
        "cwlVersion: v1.2\n"
        "class: CommandLineTool\n"
        f"label: \"ElasticBLAST re-submit (from job {job_id})\"\n"
        "doc: >\n"
        "  Re-submits a BLAST job with the source job's exact parameters.\n"
        "  Requires env: ELB_BASE_URL, ELB_TOKEN.\n"
        "requirements:\n"
        "  EnvVarRequirement:\n"
        "    envDef:\n"
        "      ELB_QUERY_FASTA: $(inputs.query_fasta.path)\n"
        "  InitialWorkDirRequirement:\n"
        "    listing:\n"
        "      - entryname: submit.py\n"
        "        entry: |\n"
        f"{embedded}\n"
        "inputs:\n"
        "  query_fasta:\n"
        "    type: File\n"
        "baseCommand: [python3, submit.py]\n"
        "stdout: submit_response.json\n"
        "outputs:\n"
        "  response:\n"
        "    type: stdout\n"
    )


def _render_wdl(job_id: str, pinned: dict) -> str:
    script = _submit_script(pinned)
    embedded = _indent(script, "        ")
    return (
        "version 1.0\n\n"
        f"# Generated by ElasticBLAST Control Plane from job {job_id}.\n"
        "# Re-submits a BLAST job with the source job's exact parameters.\n"
        "# Requires env: ELB_BASE_URL, ELB_TOKEN.\n\n"
        "workflow blast_submit {\n"
        "  input {\n"
        "    File query_fasta\n"
        "  }\n"
        "  call submit { input: query_fasta = query_fasta }\n"
        "  output {\n"
        "    File response = submit.response\n"
        "  }\n"
        "}\n\n"
        "task submit {\n"
        "  input {\n"
        "    File query_fasta\n"
        "  }\n"
        "  command <<<\n"
        "    ELB_QUERY_FASTA=~{query_fasta} python3 <<'PYEOF'\n"
        f"{embedded}"
        "PYEOF\n"
        "  >>>\n"
        "  output {\n"
        "    File response = stdout()\n"
        "  }\n"
        "}\n"
    )


_RENDERERS = {
    "nextflow": _render_nextflow,
    "snakemake": _render_snakemake,
    "cwl": _render_cwl,
    "wdl": _render_wdl,
}


def render_workflow_export(*, job_id: str, snapshot: dict, fmt: str) -> WorkflowExport:
    """Render the workflow module for ``fmt`` from a job's submit snapshot."""
    if fmt not in SUPPORTED_WORKFLOW_FORMATS:
        raise UnsupportedFormatError(f"unsupported workflow format: {fmt!r}")
    pinned = build_pinned_request(snapshot)
    content = _RENDERERS[fmt](job_id, pinned)
    return WorkflowExport(
        format=fmt,
        filename=_FORMAT_FILENAMES[fmt],
        media_type=_MEDIA_TYPE,
        content=content,
    )
