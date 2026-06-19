"""Live concurrency harness for the elb-openapi ``/v1/jobs`` service.

Responsibility: Submit N Mode-B BLAST jobs (reusing the SPA's New Search FASTA
templates) against the real elb-openapi public endpoint, then poll each job's
status on a fixed cadence and record a machine-readable timeline so we can
empirically answer: how many searches run concurrently, does queueing work, and
what is the per-query wall time (ETA signal).
Edit boundaries: This is a standalone read/execute test client run with
``uv run python``. It only talks HTTP to ``$ELB_OPENAPI_FQDN`` with the
``X-ELB-API-Token`` header. No Azure SDK, no kubectl, no FastAPI/Celery imports.
Pod-level concurrency truth is captured separately by ``watch_pods.sh``.
Key entry points: ``main`` (argparse CLI), ``resolve_db_target`` (logical
``--db`` name -> blob URL + resource profile), ``run_burst``, ``submit_job``,
``poll_once``, ``classify_state``.
Risky contracts: Each submit uses a unique ``idempotency_key`` so the server
never deduplicates two distinct test submissions into one job. The status JSON
shape is treated as opaque — ``classify_state`` scans known phase strings
defensively and falls back to ``unknown`` rather than crashing. The admin token
is read from the environment only and never logged. The harness NEVER deletes
jobs; cleanup (if any) is an explicit separate action.
Validation: ``uv run python scripts/e2e/concurrency/harness.py --help`` and a
live ``--mode single --n 1`` baseline run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_queries import QueryTemplate, load_query_templates

# State strings considered "actively consuming a compute slot" vs terminal.
# elastic-blast / k8s phases observed in the wild plus generic API phases.
# "dispatching"/"submitting"/"queued" mean elb-openapi is still preparing the
# job (no BLAST pod on a node yet); "running"/"active" mean a pod is executing.
# Keeping these buckets distinct stops the status timeline from over-reporting
# concurrency — the authoritative running count comes from watch_pods.sh.
_DISPATCH_TOKENS = ("dispatching", "submitting", "submitted", "queued", "pending", "accepted")
_RUNNING_TOKENS = ("running", "active", "in_progress")
_SUCCESS_TOKENS = ("succeeded", "success", "completed", "done", "complete")
_FAILED_TOKENS = ("failed", "error", "cancelled", "canceled", "deleted", "timeout")
_HEAVY_QUERY_IDS = ("sars-cov-2-orf1ab",)


@dataclass
class JobRecord:
    """Per-submission bookkeeping across the run."""

    index: int
    template_id: str
    length: int
    idempotency_key: str
    submit_status: int | None = None
    job_id: str | None = None
    submit_latency_s: float | None = None
    first_running_s: float | None = None
    terminal_s: float | None = None
    terminal_state: str | None = None
    last_state: str = "unsubmitted"
    error: str | None = None
    raw_submit: dict | None = None
    status_history: list[dict] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _api_base(fqdn: str) -> str:
    """Base URL for the elb-openapi service.

    ``fqdn`` may carry an explicit scheme (e.g. ``http://127.0.0.1:8000`` when
    tunnelling to the internal-only LoadBalancer via ``kubectl port-forward``);
    otherwise default to ``https://`` for a public HTTPS endpoint.
    """
    if fqdn.startswith(("http://", "https://")):
        return fqdn.rstrip("/")
    return f"https://{fqdn}"


def classify_state(payload: dict) -> str:
    """Collapse an opaque status payload into running/succeeded/failed/unknown."""

    # Gather every plausible phase/status string in the payload.
    candidates: list[str] = []
    for key in ("status", "phase", "state", "job_status", "elastic_blast_status", "stage"):
        val = payload.get(key)
        if isinstance(val, str):
            candidates.append(val.lower())
    # Nested ``status`` object (e.g. {"status": {"phase": "..."}}).
    nested = payload.get("status")
    if isinstance(nested, dict):
        for key in ("phase", "state", "status"):
            val = nested.get(key)
            if isinstance(val, str):
                candidates.append(val.lower())
    blob = " ".join(candidates)
    if not blob:
        blob = json.dumps(payload).lower()
    if any(t in blob for t in _SUCCESS_TOKENS):
        return "succeeded"
    if any(t in blob for t in _FAILED_TOKENS):
        return "failed"
    if any(t in blob for t in _RUNNING_TOKENS):
        return "running"
    if any(t in blob for t in _DISPATCH_TOKENS):
        return "dispatching"
    return "unknown"


def _extract_job_id(payload: dict) -> str | None:
    for key in ("job_id", "id", "jobId", "name", "handle"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    # Nested handle.
    for key in ("job", "data", "result"):
        sub = payload.get(key)
        if isinstance(sub, dict):
            got = _extract_job_id(sub)
            if got:
                return got
    return None


def _default_resource_profile(db: str) -> str | None:
    """Pick the elb-openapi resource profile a logical DB needs to shard safely.

    ``core_nt`` is too large to load whole into one E16s_v5 node (the standard,
    non-sharded path fails the elastic-blast memory pre-flight asking for a
    ~252GB machine). The ``core_nt_safe`` profile is what the cluster's working
    sharded runs (10 shards, one per node) used, so default to it. Small DBs
    (16S etc.) fit in memory and need no special profile.
    """

    return "core_nt_safe" if "core_nt" in db else None


def _default_blast_options(db: str) -> dict | None:
    """Pick ``blast_options`` a logical DB needs to merge sharded results.

    ``POST /v1/jobs`` Mode B (inline ``query_fasta``) ignores the raw ``options``
    string and instead renders BLAST flags from the structured ``blast_options``
    object; with no ``outfmt`` it defaults to ``-outfmt 7``. A sharded DB
    (``core_nt`` = 10 shards) merges per-shard hits at the end and elastic-blast's
    merge step rejects outfmt 7 — only outfmt 5 (no extended fields), 6, or
    ``"6 std..."`` are mergeable. We pin plain ``outfmt 5`` (XML, no extended
    columns). Small single-shard DBs (16S) are not partitioned and keep the
    server default.
    """

    if "core_nt" in db:
        return {"outfmt": "5"}
    return None


def resolve_db_target(
    *,
    fqdn: str,
    token: str,
    db: str,
    db_url: str | None,
    resource_profile: str | None,
    blast_options: dict | None,
) -> tuple[str, str | None, dict | None]:
    """Turn a logical ``--db`` name into the actual submit ``db`` + profile + blast_options.

    Precedence: an explicit ``--db-url`` wins; a ``--db`` that already looks like
    a blob URL is used as-is; otherwise build the canonical
    ``https://<account>.blob.core.windows.net/<container>/<db>/<db>`` URL from the
    elb-openapi ``/v1/config`` storage coordinates. The resource profile and
    structured BLAST options default per :func:`_default_resource_profile` /
    :func:`_default_blast_options` unless overridden on the CLI.
    """

    profile = resource_profile if resource_profile is not None else _default_resource_profile(db)
    resolved_opts = blast_options if blast_options is not None else _default_blast_options(db)
    if db_url:
        return db_url, profile, resolved_opts
    if db.startswith(("http://", "https://")):
        return db, profile, resolved_opts
    resp = httpx.get(
        f"{_api_base(fqdn)}/v1/config",
        headers={"X-ELB-API-Token": token},
        timeout=30.0,
    )
    resp.raise_for_status()
    cp = resp.json().get("cloud-provider", {})
    account = cp.get("azure-storage-account")
    container = cp.get("azure-storage-account-container", "blast-db")
    if not account:
        raise RuntimeError("/v1/config missing azure-storage-account; pass --db-url explicitly")
    return (
        f"https://{account}.blob.core.windows.net/{container}/{db}/{db}",
        profile,
        resolved_opts,
    )


async def submit_job(
    client: httpx.AsyncClient,
    *,
    fqdn: str,
    token: str,
    db: str,
    template: QueryTemplate,
    record: JobRecord,
    t0: float,
    resource_profile: str | None = None,
    blast_options: dict | None = None,
    submit_timeout: float = 60.0,
) -> None:
    """POST one Mode-B job and stamp submit latency + job id onto the record."""

    body: dict = {
        "program": template.program,
        "db": db,
        "query_fasta": template.fasta,
        "idempotency_key": record.idempotency_key,
        "priority": 50,
        "submission_source": "external_api",
        "external_correlation_id": record.idempotency_key,
    }
    if resource_profile:
        body["resource_profile"] = resource_profile
    if blast_options:
        body["blast_options"] = blast_options
    started = time.monotonic()
    try:
        resp = await client.post(
            f"{_api_base(fqdn)}/v1/jobs",
            json=body,
            headers={"X-ELB-API-Token": token},
            timeout=submit_timeout,
        )
        record.submit_latency_s = round(time.monotonic() - started, 3)
        record.submit_status = resp.status_code
        try:
            payload = resp.json()
        except Exception:
            payload = {"_raw_text": resp.text[:500]}
        record.raw_submit = payload
        if resp.status_code in (200, 201, 202):
            record.job_id = _extract_job_id(payload)
            record.last_state = "submitted"
        else:
            record.error = f"http {resp.status_code}: {json.dumps(payload)[:200]}"
            record.last_state = "submit_failed"
    except Exception as exc:  # network / timeout
        record.submit_latency_s = round(time.monotonic() - started, 3)
        record.error = f"submit_exc: {exc!r}"
        record.last_state = "submit_exc"


async def poll_once(
    client: httpx.AsyncClient, *, fqdn: str, token: str, record: JobRecord, t0: float
) -> None:
    """GET one job's status and append a timeline sample to its history."""

    if not record.job_id:
        return
    elapsed = round(time.monotonic() - t0, 3)
    try:
        resp = await client.get(
            f"{_api_base(fqdn)}/v1/jobs/{record.job_id}/status",
            headers={"X-ELB-API-Token": token},
            timeout=30.0,
        )
        try:
            payload = resp.json()
        except Exception:
            payload = {"_raw_text": resp.text[:300]}
        if resp.status_code == 200:
            state = classify_state(payload)
        else:
            state = f"http_{resp.status_code}"
    except Exception as exc:
        payload = {"_exc": repr(exc)}
        state = "poll_exc"
    record.last_state = state
    record.status_history.append({"t": elapsed, "ts": _now_iso(), "state": state, "raw": payload})
    if state == "running" and record.first_running_s is None:
        record.first_running_s = elapsed
    if state in ("succeeded", "failed") and record.terminal_s is None:
        record.terminal_s = elapsed
        record.terminal_state = state


def _is_terminal(record: JobRecord) -> bool:
    return record.terminal_state in ("succeeded", "failed") or record.last_state in (
        "submit_failed",
        "submit_exc",
    )


async def run_burst(
    *,
    fqdn: str,
    token: str,
    db: str,
    templates: list[QueryTemplate],
    n: int,
    poll_s: float,
    wall_timeout_s: float,
    outdir: Path,
    resource_profile: str | None = None,
    blast_options: dict | None = None,
    submit_concurrency: int = 0,
    submit_timeout: float = 60.0,
) -> dict:
    """Submit ``n`` jobs ~simultaneously, poll all until terminal/timeout."""

    run_id = uuid.uuid4().hex[:8]
    selected: list[QueryTemplate] = [templates[i % len(templates)] for i in range(n)]
    records = [
        JobRecord(
            index=i,
            template_id=t.id,
            length=t.length,
            idempotency_key=f"conc-{run_id}-{i:03d}-{t.id}",
        )
        for i, t in enumerate(selected)
    ]
    outdir.mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": run_id,
        "started_at": _now_iso(),
        "fqdn": fqdn,
        "db": db,
        "resource_profile": resource_profile,
        "blast_options": blast_options,
        "n": n,
        "poll_s": poll_s,
        "wall_timeout_s": wall_timeout_s,
        "queries": [
            {"index": r.index, "template_id": r.template_id, "length": r.length} for r in records
        ],
    }
    (outdir / "run_meta.json").write_text(json.dumps(meta, indent=2))
    print(
        f"[{_now_iso()}] run {run_id}: bursting {n} submits to {_api_base(fqdn)}/v1/jobs",
        flush=True,
    )

    t0 = time.monotonic()
    limits = httpx.Limits(max_connections=max(n, 10), max_keepalive_connections=max(n, 10))
    async with httpx.AsyncClient(limits=limits, http2=False) as client:
        # Fire submits, optionally capped by a semaphore so a rate-limited
        # openapi (or a single kubectl port-forward tunnel) is not flooded with
        # all N at once — each submit still lands within a short window, which
        # is what populates the server-side queue we are measuring.
        sem = asyncio.Semaphore(submit_concurrency) if submit_concurrency > 0 else None

        async def _submit_one(t: QueryTemplate, r: JobRecord) -> None:
            if sem is not None:
                async with sem:
                    await submit_job(
                        client,
                        fqdn=fqdn,
                        token=token,
                        db=db,
                        template=t,
                        record=r,
                        t0=t0,
                        resource_profile=resource_profile,
                        blast_options=blast_options,
                        submit_timeout=submit_timeout,
                    )
            else:
                await submit_job(
                    client,
                    fqdn=fqdn,
                    token=token,
                    db=db,
                    template=t,
                    record=r,
                    t0=t0,
                    resource_profile=resource_profile,
                    blast_options=blast_options,
                    submit_timeout=submit_timeout,
                )

        await asyncio.gather(
            *(_submit_one(t, r) for t, r in zip(selected, records, strict=True))
        )
        submits_path = outdir / "submits.ndjson"
        with submits_path.open("w") as fh:
            for r in records:
                fh.write(
                    json.dumps(
                        {
                            "index": r.index,
                            "template_id": r.template_id,
                            "idempotency_key": r.idempotency_key,
                            "submit_status": r.submit_status,
                            "job_id": r.job_id,
                            "submit_latency_s": r.submit_latency_s,
                            "error": r.error,
                            "raw_submit": r.raw_submit,
                        }
                    )
                    + "\n"
                )
        ok = [r for r in records if r.job_id]
        print(
            f"[{_now_iso()}] submitted ok={len(ok)}/{n} "
            f"statuses={sorted({r.submit_status for r in records}, key=lambda s: (s is None, s))}",
            flush=True,
        )

        # Poll loop.
        status_path = outdir / "status.ndjson"
        max_running = 0
        with status_path.open("w") as fh:
            while True:
                elapsed = time.monotonic() - t0
                await asyncio.gather(
                    *(poll_once(client, fqdn=fqdn, token=token, record=r, t0=t0) for r in ok)
                )
                running_now = sum(1 for r in ok if r.last_state == "running")
                max_running = max(max_running, running_now)
                states = {}
                for r in records:
                    states[r.last_state] = states.get(r.last_state, 0) + 1
                fh.write(
                    json.dumps(
                        {
                            "t": round(elapsed, 2),
                            "ts": _now_iso(),
                            "running_now": running_now,
                            "states": states,
                        }
                    )
                    + "\n"
                )
                fh.flush()
                print(
                    f"[{_now_iso()}] t={elapsed:6.1f}s running={running_now} "
                    f"max_running={max_running} {states}",
                    flush=True,
                )
                if ok and all(_is_terminal(r) for r in ok):
                    print(f"[{_now_iso()}] all jobs terminal", flush=True)
                    break
                if elapsed > wall_timeout_s:
                    print(f"[{_now_iso()}] wall timeout {wall_timeout_s}s reached", flush=True)
                    break
                await asyncio.sleep(poll_s)

    summary = {
        "run_id": run_id,
        "finished_at": _now_iso(),
        "n": n,
        "submitted_ok": len([r for r in records if r.job_id]),
        "max_concurrent_running_status": max_running,
        "jobs": [
            {
                "index": r.index,
                "template_id": r.template_id,
                "length": r.length,
                "job_id": r.job_id,
                "submit_status": r.submit_status,
                "submit_latency_s": r.submit_latency_s,
                "first_running_s": r.first_running_s,
                "terminal_s": r.terminal_s,
                "terminal_state": r.terminal_state,
                "last_state": r.last_state,
                "error": r.error,
            }
            for r in records
        ],
    }
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2))
    # Full per-job status history for deep analysis.
    (outdir / "jobs_history.json").write_text(
        json.dumps(
            {r.job_id or f"idx{r.index}": r.status_history for r in records},
            indent=1,
        )
    )
    print(f"[{_now_iso()}] summary -> {outdir / 'summary.json'}", flush=True)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="elb-openapi /v1/jobs concurrency harness")
    ap.add_argument("--mode", choices=["single", "burst"], default="burst")
    ap.add_argument("--n", type=int, default=10, help="number of concurrent submits (burst)")
    ap.add_argument("--db", default="core_nt")
    ap.add_argument(
        "--db-url",
        default="",
        help="explicit blob URL sent as the submit 'db' (overrides --db resolution)",
    )
    ap.add_argument(
        "--resource-profile",
        default=None,
        help="elb-openapi resource profile (e.g. core_nt_safe); default inferred from db",
    )
    ap.add_argument(
        "--outfmt",
        default=None,
        help="BLAST outfmt sent as blast_options.outfmt (e.g. 5); default inferred from db",
    )
    ap.add_argument("--poll", type=float, default=10.0, help="poll cadence seconds")
    ap.add_argument("--wall-timeout", type=float, default=2400.0, help="max wall seconds")
    ap.add_argument(
        "--submit-concurrency",
        type=int,
        default=0,
        help="max in-flight submits (0 = all at once); use a small value (e.g. 5) "
        "when tunnelling through a single kubectl port-forward to a rate-limited openapi",
    )
    ap.add_argument(
        "--submit-timeout",
        type=float,
        default=60.0,
        help="per-submit HTTP timeout seconds (raise when each submit is slow, e.g. core_nt)",
    )
    ap.add_argument("--include-heavy", action="store_true", help="include orf1ab (21kb) query")
    ap.add_argument("--outdir", default="", help="output dir (default .logs/e2e/concurrency/<ts>)")
    args = ap.parse_args()

    fqdn = os.environ.get("ELB_OPENAPI_FQDN", "").strip()
    token = os.environ.get("X_ELB_API_TOKEN", "").strip()
    if not fqdn or not token:
        print("ERROR: set ELB_OPENAPI_FQDN and X_ELB_API_TOKEN env vars", file=sys.stderr)
        return 2

    templates = load_query_templates(database=args.db)
    if not args.include_heavy:
        templates = [t for t in templates if t.id not in _HEAVY_QUERY_IDS]
    # Stable, light-first ordering so single/baseline picks a small query.
    templates.sort(key=lambda t: t.length)
    if not templates:
        print("ERROR: no templates for db", args.db, file=sys.stderr)
        return 2

    n = 1 if args.mode == "single" else args.n
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.outdir) if args.outdir else Path(".logs/e2e/concurrency") / ts

    try:
        cli_blast_options = {"outfmt": args.outfmt} if args.outfmt else None
        submit_db, resource_profile, blast_options = resolve_db_target(
            fqdn=fqdn,
            token=token,
            db=args.db,
            db_url=args.db_url or None,
            resource_profile=args.resource_profile,
            blast_options=cli_blast_options,
        )
    except Exception as exc:
        print(f"ERROR: could not resolve db target: {exc!r}", file=sys.stderr)
        return 2
    print(
        f"[{_now_iso()}] db '{args.db}' -> submit db={submit_db} "
        f"resource_profile={resource_profile} blast_options={blast_options!r}",
        flush=True,
    )

    summary = asyncio.run(
        run_burst(
            fqdn=fqdn,
            token=token,
            db=submit_db,
            templates=templates,
            n=n,
            poll_s=args.poll,
            wall_timeout_s=args.wall_timeout,
            outdir=outdir,
            resource_profile=resource_profile,
            blast_options=blast_options,
            submit_concurrency=args.submit_concurrency,
            submit_timeout=args.submit_timeout,
        )
    )
    print(
        json.dumps(
            {
                "max_concurrent_running_status": summary["max_concurrent_running_status"],
                "submitted_ok": summary["submitted_ok"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
