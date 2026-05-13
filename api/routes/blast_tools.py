"""BLAST utility routes — cost estimation, taxonomy, preprocess, primer design, schedules, DB versions."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid

import azure.durable_functions as df
import azure.functions as func

from _http_utils import (
    _RE_DB_NAME,
    _RE_STORAGE_ACCOUNT,
    _error_response,
    _json_response,
    _require_query,
    _validate_name,
    _validate_rg,
    _validate_sub,
    resolve_terminal_secret,
)
from auth.token import AuthError, validate_bearer_token
from models.blast import BlastSubmitRequest
from services import compute as compute_svc
from services import keyvault as kv_svc
from services import storage_data as storage_data_svc
from services.azure_clients import credential_for_caller
from services.sanitise import sanitise
from services.blast_config import AZURE_VM_HOURLY_USD as _AZURE_VM_HOURLY_USD
from services.blast_config import PD_GB_MONTH_USD as _PD_GB_MONTH_USD
from services.blast_config import STORAGE_GB_MONTH_USD as _STORAGE_GB_MONTH_USD

LOGGER = logging.getLogger(__name__)

bp = df.Blueprint()

@bp.route(route="blast/cost-estimate", methods=["POST"])
def blast_cost_estimate(req: func.HttpRequest) -> func.HttpResponse:
    """Estimate Azure cost for a BLAST job."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    node_sku = body.get("machine_type", "Standard_E16s_v5")
    num_nodes = max(1, min(int(body.get("num_nodes", 3)), 100))
    estimated_hours = max(0.1, min(float(body.get("estimated_hours", 2.0)), 168))
    pd_size_gb = max(10, min(int(body.get("pd_size_gb", 1000)), 10000))
    db_size_gb = float(body.get("db_size_gb", 50))

    hourly = _AZURE_VM_HOURLY_USD.get(node_sku, 1.0)

    compute_cost = hourly * num_nodes * estimated_hours
    disk_cost = (pd_size_gb * _PD_GB_MONTH_USD / 730) * estimated_hours * num_nodes
    storage_cost = db_size_gb * _STORAGE_GB_MONTH_USD / 730 * estimated_hours
    total = compute_cost + disk_cost + storage_cost

    return _json_response(
        {
            "estimate": {
                "compute_usd": round(compute_cost, 2),
                "disk_usd": round(disk_cost, 2),
                "storage_usd": round(storage_cost, 2),
                "total_usd": round(total, 2),
            },
            "params": {
                "node_sku": node_sku,
                "num_nodes": num_nodes,
                "estimated_hours": estimated_hours,
                "pd_size_gb": pd_size_gb,
                "hourly_rate_usd": hourly,
            },
            "note": "Estimate based on pay-as-you-go pricing. Actual costs may vary.",
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# P8 — Multi-DB Search
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/multi-submit", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def blast_multi_db_submit(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    """Submit the same query against multiple databases simultaneously."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    databases: list[str] = body.get("databases", [])
    if not databases or len(databases) < 2:
        return _error_response(400, "provide at least 2 databases")
    if len(databases) > 10:
        return _error_response(400, "max 10 databases per multi-search")

    # Validate each DB name
    for db in databases:
        if not db or len(db) > 200:
            return _error_response(400, f"invalid database name: {sanitise(db[:40])}")

    # Create a job for each database
    group_id = f"multi-{uuid.uuid4().hex[:12]}"
    jobs: list[dict] = []

    for db_name in databases:
        job_body = dict(body)
        job_body["db"] = db_name
        job_body.pop("databases", None)

        try:
            submit_req = BlastSubmitRequest(**job_body)
        except ValidationError as exc:
            return _error_response(400, f"DB '{sanitise(db_name[:30])}': {exc.errors()[0]['msg']}")

        job_id = f"{group_id}-{db_name.split('/')[-1]}"
        payload = submit_req.model_dump()
        payload["job_id"] = job_id
        payload["user_assertion"] = identity.raw_token
        payload["owner_upn"] = identity.upn
        payload["owner_oid"] = identity.oid
        payload["group_id"] = group_id

        instance_id = await client.start_new("submit_blast_orchestrator", None, payload)
        jobs.append({"job_id": job_id, "db": db_name, "instance_id": instance_id})

    return _json_response(
        {
            "group_id": group_id,
            "jobs": jobs,
            "total": len(jobs),
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# P4 — Taxonomy Annotation
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/taxonomy", methods=["POST"])
def blast_taxonomy_lookup(req: func.HttpRequest) -> func.HttpResponse:
    """Look up NCBI taxonomy for subject accessions from BLAST results."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    accessions: list[str] = body.get("accessions", [])
    if not accessions:
        return _error_response(400, "provide accessions list")
    # Cap at 50 accessions per request
    accessions = accessions[:50]

    # Validate accession format
    _RE_ACCESSION = re.compile(r"^[A-Za-z0-9_.]+$")
    for acc in accessions:
        if not _RE_ACCESSION.match(acc):
            return _error_response(400, f"invalid accession: {sanitise(acc[:40])}")

    results: dict[str, dict] = {}
    try:
        # Use NCBI E-utilities to look up taxonomy
        # esummary for nucleotide/protein database
        ids_param = ",".join(accessions)
        # First: convert accession to GI via esearch
        esearch_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
            f"?db=nucleotide&id={ids_param}&retmode=json"
        )
        resp = _requests.get(esearch_url, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            uid_list = data.get("result", {}).get("uids", [])
            for uid in uid_list:
                info = data["result"].get(uid, {})
                acc_ver = info.get("accessionversion", info.get("caption", uid))
                results[acc_ver] = {
                    "accession": acc_ver,
                    "title": info.get("title", ""),
                    "organism": info.get("organism", ""),
                    "taxid": info.get("taxid", ""),
                    "source_db": info.get("sourcedb", ""),
                    "seq_length": info.get("slen", ""),
                    "mol_type": info.get("moltype", ""),
                    "update_date": info.get("updatedate", ""),
                }
    except Exception as exc:
        LOGGER.warning("NCBI taxonomy lookup failed: %s", exc)

    # Also try protein DB for any missing
    missing = [a for a in accessions if a not in results]
    if missing:
        try:
            ids_param = ",".join(missing)
            prot_url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
                f"?db=protein&id={ids_param}&retmode=json"
            )
            resp = _requests.get(prot_url, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                for uid in data.get("result", {}).get("uids", []):
                    info = data["result"].get(uid, {})
                    acc_ver = info.get("accessionversion", info.get("caption", uid))
                    results[acc_ver] = {
                        "accession": acc_ver,
                        "title": info.get("title", ""),
                        "organism": info.get("organism", ""),
                        "taxid": info.get("taxid", ""),
                        "source_db": info.get("sourcedb", ""),
                        "seq_length": info.get("slen", ""),
                    }
        except Exception:
            LOGGER.debug("protein taxonomy lookup failed", exc_info=True)

    return _json_response(
        {
            "annotations": results,
            "found": len(results),
            "requested": len(accessions),
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# P5 — Query Preprocessor (FASTQ→FASTA, stats)
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/preprocess", methods=["POST"])
def blast_preprocess_query(req: func.HttpRequest) -> func.HttpResponse:
    """Preprocess query sequences: FASTQ→FASTA conversion, quality stats, filtering."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    input_data: str = body.get("input_data", "")
    if not input_data:
        return _error_response(400, "provide input_data")
    if len(input_data) > 50 * 1024 * 1024:  # 50 MB
        return _error_response(400, "input too large (max 50 MB)")

    input_format = body.get("format", "auto")  # auto, fastq, fasta
    min_length = int(body.get("min_length", 0))
    min_quality = int(body.get("min_quality", 0))

    lines = input_data.splitlines()

    # Auto-detect format
    if input_format == "auto":
        if lines and lines[0].startswith("@"):
            input_format = "fastq"
        elif lines and lines[0].startswith(">"):
            input_format = "fasta"
        else:
            return _error_response(
                400, "cannot detect format — must start with > (FASTA) or @ (FASTQ)"
            )

    fasta_seqs: list[dict] = []
    stats = {
        "input_sequences": 0,
        "output_sequences": 0,
        "total_bases": 0,
        "filtered_short": 0,
        "filtered_quality": 0,
        "avg_length": 0.0,
        "min_len": 0,
        "max_len": 0,
        "gc_content": 0.0,
    }

    if input_format == "fastq":
        # Parse FASTQ (4 lines per record: @id, seq, +, qual)
        i = 0
        lengths: list[int] = []
        gc_count = 0
        while i + 3 < len(lines):
            header = lines[i].strip()
            seq = lines[i + 1].strip()
            # lines[i+2] is "+"
            qual = lines[i + 3].strip()
            i += 4

            if not header.startswith("@"):
                continue
            stats["input_sequences"] += 1

            # Quality filtering
            if min_quality > 0 and qual:
                avg_q = sum(ord(c) - 33 for c in qual) / max(len(qual), 1)
                if avg_q < min_quality:
                    stats["filtered_quality"] += 1
                    continue

            # Length filtering
            if min_length > 0 and len(seq) < min_length:
                stats["filtered_short"] += 1
                continue

            seq_id = header[1:].split()[0]
            desc = header[1:].split(None, 1)[1] if " " in header[1:] else ""
            fasta_seqs.append({"id": seq_id, "desc": desc, "seq": seq})
            lengths.append(len(seq))
            gc_count += seq.upper().count("G") + seq.upper().count("C")
            stats["total_bases"] += len(seq)

        if lengths:
            stats["min_len"] = min(lengths)
            stats["max_len"] = max(lengths)
            stats["avg_length"] = round(sum(lengths) / len(lengths), 1)
            stats["gc_content"] = (
                round(gc_count / stats["total_bases"] * 100, 1) if stats["total_bases"] > 0 else 0
            )

    elif input_format == "fasta":
        # Parse FASTA
        current_id = ""
        current_desc = ""
        current_seq: list[str] = []
        lengths = []
        gc_count = 0

        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if current_id and current_seq:
                    seq = "".join(current_seq)
                    stats["input_sequences"] += 1
                    if min_length > 0 and len(seq) < min_length:
                        stats["filtered_short"] += 1
                    else:
                        fasta_seqs.append({"id": current_id, "desc": current_desc, "seq": seq})
                        lengths.append(len(seq))
                        gc_count += seq.upper().count("G") + seq.upper().count("C")
                        stats["total_bases"] += len(seq)
                parts = line[1:].split(None, 1)
                current_id = parts[0] if parts else ""
                current_desc = parts[1] if len(parts) > 1 else ""
                current_seq = []
            else:
                current_seq.append(line)

        # Last sequence
        if current_id and current_seq:
            seq = "".join(current_seq)
            stats["input_sequences"] += 1
            if min_length > 0 and len(seq) < min_length:
                stats["filtered_short"] += 1
            else:
                fasta_seqs.append({"id": current_id, "desc": current_desc, "seq": seq})
                lengths.append(len(seq))
                gc_count += seq.upper().count("G") + seq.upper().count("C")
                stats["total_bases"] += len(seq)

        if lengths:
            stats["min_len"] = min(lengths)
            stats["max_len"] = max(lengths)
            stats["avg_length"] = round(sum(lengths) / len(lengths), 1)
            stats["gc_content"] = (
                round(gc_count / stats["total_bases"] * 100, 1) if stats["total_bases"] > 0 else 0
            )

    stats["output_sequences"] = len(fasta_seqs)

    # Build output FASTA
    fasta_output_lines: list[str] = []
    for s in fasta_seqs:
        header = f">{s['id']}"
        if s["desc"]:
            header += f" {s['desc']}"
        fasta_output_lines.append(header)
        # Wrap sequence at 80 chars
        seq = s["seq"]
        for j in range(0, len(seq), 80):
            fasta_output_lines.append(seq[j : j + 80])

    return _json_response(
        {
            "fasta_output": "\n".join(fasta_output_lines),
            "stats": stats,
            "detected_format": input_format,
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# P2 — DB Version Registry
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/databases/versions", methods=["GET"])
def list_db_versions(req: func.HttpRequest) -> func.HttpResponse:
    """List all database versions (based on metadata.json files in blast-db container)."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    params, err = _require_query(req, "subscription_id", "storage_account", "resource_group")
    if err:
        return err

    cred = credential_for_caller(identity.raw_token)

    try:
        svc = storage_data_svc._blob_service(cred, params["storage_account"])
        cc = svc.get_container_client("blast-db")

        versions: list[dict] = []
        for blob in cc.list_blobs():
            if blob.name.endswith("-metadata.json") or blob.name.endswith("/metadata.json"):
                try:
                    bc = cc.get_blob_client(blob.name)
                    raw = bc.download_blob().readall().decode("utf-8")
                    meta = json.loads(raw)
                    meta["_blob_path"] = blob.name
                    meta["_last_modified"] = (
                        blob.last_modified.isoformat() if blob.last_modified else None
                    )
                    meta["_size_bytes"] = blob.size
                    versions.append(meta)
                except Exception:
                    LOGGER.debug("failed to read database metadata blob", exc_info=True)

        # Sort by created_at or downloaded_at
        versions.sort(
            key=lambda v: v.get("created_at") or v.get("downloaded_at") or "",
            reverse=True,
        )
        return _json_response({"versions": versions, "total": len(versions)})
    except Exception as exc:
        msg = str(exc)
        if "AuthorizationFailure" in msg or "PublicAccessNotPermitted" in msg or "AuthorizationPermission" in msg:
            # Storage public access is disabled — return empty list with a note
            return _json_response({"versions": [], "total": 0, "public_access_disabled": True})
        return _error_response(500, sanitise(msg[:500]))


@bp.route(route="blast/databases/versions", methods=["POST"])
def save_db_version_metadata(req: func.HttpRequest) -> func.HttpResponse:
    """Save or update metadata for a database version."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    sub = body.get("subscription_id", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)
    storage_account = body.get("storage_account", "")
    if err := _validate_name(storage_account, _RE_STORAGE_ACCOUNT, "storage_account"):
        return _error_response(400, err)
    db_name = body.get("db_name", "")
    if err := _validate_name(db_name, _RE_DB_NAME, "db_name"):
        return _error_response(400, err)

    cred = credential_for_caller(identity.raw_token)
    import datetime as _dt

    metadata = {
        "db_name": db_name,
        "db_type": body.get("db_type", "unknown"),
        "title": body.get("title", db_name),
        "source": body.get("source", "custom"),
        "source_version": body.get("source_version", ""),
        "version_tag": body.get("version_tag", ""),
        "notes": body.get("notes", ""),
        "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "created_by": identity.upn or identity.oid,
    }

    try:
        blob_path = f"{db_name}/{db_name}-metadata.json"
        storage_data_svc.upload_query_text(
            cred,
            storage_account,
            "blast-db",
            blob_path,
            json.dumps(metadata, indent=2),
        )
        return _json_response({"db_name": db_name, "status": "saved", "metadata": metadata})
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)[:500]))


# ═══════════════════════════════════════════════════════════════════════
# P7 — Scheduled / Triggered BLAST routes
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/schedules", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def list_blast_schedules(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    """List all scheduled BLAST configurations."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        entity_id = df.EntityId("scheduled_blast_entity", "global")
        resp = await client.read_entity_state(entity_id)
        schedules = resp.entity_state if resp.entity_exists else []
    except Exception:
        schedules = []

    return _json_response({"schedules": schedules})


@bp.route(route="blast/schedules", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def create_blast_schedule(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    """Create a scheduled/triggered BLAST job configuration."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    name = body.get("name", "")
    if not name or len(name) > 100:
        return _error_response(400, "provide a schedule name (max 100 chars)")

    trigger_type = body.get("trigger_type", "manual")  # manual, cron, on_upload
    if trigger_type not in ("manual", "cron", "on_upload"):
        return _error_response(400, "trigger_type must be manual, cron, or on_upload")

    schedule_config = {
        "schedule_id": uuid.uuid4().hex[:12],
        "name": name,
        "trigger_type": trigger_type,
        "cron_expression": body.get("cron_expression", ""),
        "watch_container": body.get("watch_container", "queries"),
        "watch_prefix": body.get("watch_prefix", ""),
        "blast_params": {
            k: v
            for k, v in body.items()
            if k
            not in ("name", "trigger_type", "cron_expression", "watch_container", "watch_prefix")
        },
        "owner_upn": identity.upn,
    }

    entity_id = df.EntityId("scheduled_blast_entity", "global")
    await client.signal_entity(entity_id, "add_schedule", schedule_config)

    return _json_response({"status": "created", "schedule": schedule_config})


@bp.route(route="blast/schedules/{schedule_id}", methods=["DELETE"])
@bp.durable_client_input(client_name="client")
async def delete_blast_schedule(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    """Delete a scheduled BLAST configuration."""
    try:
        validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    schedule_id = req.route_params.get("schedule_id", "")
    if not schedule_id:
        return _error_response(400, "schedule_id required")

    entity_id = df.EntityId("scheduled_blast_entity", "global")
    await client.signal_entity(entity_id, "remove_schedule", {"schedule_id": schedule_id})

    return _json_response({"status": "deleted", "schedule_id": schedule_id})


@bp.route(route="blast/schedules/{schedule_id}/run", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def run_blast_schedule(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    """Manually trigger a scheduled BLAST job."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    schedule_id = req.route_params.get("schedule_id", "")
    if not schedule_id:
        return _error_response(400, "schedule_id required")


    # Read schedule config
    try:
        entity_id = df.EntityId("scheduled_blast_entity", "global")
        resp = await client.read_entity_state(entity_id)
        schedules = resp.entity_state if resp.entity_exists else []
    except Exception:
        return _error_response(404, "no schedules found")

    target = next((s for s in schedules if s.get("schedule_id") == schedule_id), None)
    if not target:
        return _error_response(404, f"schedule {sanitise(schedule_id)} not found")

    # Submit a BLAST job using the schedule's params
    blast_params = target.get("blast_params", {})
    try:
        submit_req = BlastSubmitRequest(**blast_params)
    except ValidationError as exc:
        return _error_response(400, f"invalid schedule params: {exc.errors()[0]['msg']}")

    job_id = f"sched-{schedule_id}-{uuid.uuid4().hex[:8]}"
    payload = submit_req.model_dump()
    payload["job_id"] = job_id
    payload["user_assertion"] = identity.raw_token
    payload["owner_upn"] = identity.upn
    payload["owner_oid"] = identity.oid
    payload["schedule_id"] = schedule_id

    instance_id = await client.start_new("submit_blast_orchestrator", None, payload)

    # Mark run
    await client.signal_entity(entity_id, "mark_run", {"schedule_id": schedule_id})

    return _json_response(
        {
            "job_id": job_id,
            "instance_id": instance_id,
            "schedule_id": schedule_id,
        }
    )


# ═══════════════════════════════════════════════════════════════════════
# P12 — Primer Design (Primer3 on Terminal VM)
# ═══════════════════════════════════════════════════════════════════════
@bp.route(route="blast/primer-design", methods=["POST"])
def blast_primer_design(req: func.HttpRequest) -> func.HttpResponse:
    """Design PCR primers for a target region using Primer3 on the Terminal VM."""
    try:
        identity = validate_bearer_token(req.headers.get("Authorization"))
    except AuthError as exc:
        return _error_response(exc.status, exc.message)

    try:
        body = req.get_json()
    except Exception:
        return _error_response(400, "invalid JSON")

    sequence = body.get("sequence", "")
    if not sequence or len(sequence) < 50:
        return _error_response(400, "provide a sequence of at least 50 bp")
    if len(sequence) > 100000:
        return _error_response(400, "sequence too long (max 100 kb)")

    # Validate sequence content
    clean_seq = re.sub(r"\s", "", sequence).upper()
    if not re.match(r"^[ATGCNRYSWKMBDHVU]+$", clean_seq):
        return _error_response(400, "invalid nucleotide characters in sequence")

    target_start = int(body.get("target_start", max(1, len(clean_seq) // 4)))
    target_length = int(body.get("target_length", min(200, len(clean_seq) // 2)))
    product_min = int(body.get("product_size_min", 100))
    product_max = int(body.get("product_size_max", 1000))
    num_return = min(int(body.get("num_return", 5)), 10)

    sub = body.get("subscription_id", "")
    if err := _validate_sub(sub):
        return _error_response(400, err)

    terminal_rg = body.get(
        "terminal_resource_group", os.environ.get("TERMINAL_DEFAULT_RG", "rg-elb-terminal")
    )
    terminal_vm = body.get("terminal_vm_name", "vm-elb-terminal")

    cred = credential_for_caller(identity.raw_token)

    try:
        vm_ip = compute_svc.get_vm_public_ip(cred, sub, terminal_rg, terminal_vm)
        if not vm_ip:
            return _error_response(400, "Terminal VM not available")

        secret_name = f"vm-{terminal_vm}-password"
        password, _ = resolve_terminal_secret(cred, sub, terminal_rg, terminal_vm, secret_name)
        if not password:
            return _error_response(404, "VM password not found in Key Vault")

        from services.ssh_exec import run_ssh

        # Build Primer3 boulder-IO input
        primer3_input = f"""SEQUENCE_TEMPLATE={clean_seq}
SEQUENCE_TARGET={target_start},{target_length}
PRIMER_TASK=generic
PRIMER_PICK_LEFT_PRIMER=1
PRIMER_PICK_RIGHT_PRIMER=1
PRIMER_NUM_RETURN={num_return}
PRIMER_PRODUCT_SIZE_RANGE={product_min}-{product_max}
PRIMER_MIN_SIZE=18
PRIMER_OPT_SIZE=20
PRIMER_MAX_SIZE=25
PRIMER_MIN_TM=57.0
PRIMER_OPT_TM=60.0
PRIMER_MAX_TM=63.0
PRIMER_MIN_GC=40.0
PRIMER_MAX_GC=60.0
="""

        script = f"""set -euo pipefail
# Check if primer3_core is available
if ! command -v primer3_core &>/dev/null; then
  sudo apt-get update -qq && sudo apt-get install -y -qq primer3 2>&1 | tail -3
fi

# Run primer3
echo '{primer3_input}' | primer3_core 2>&1
"""
        output = run_ssh(vm_ip, password, script, timeout=120)

        # Parse Primer3 output
        primers = _parse_primer3_output(output)

        return _json_response(
            {
                "primers": primers,
                "target": {"start": target_start, "length": target_length},
                "product_size_range": f"{product_min}-{product_max}",
                "sequence_length": len(clean_seq),
            }
        )
    except Exception as exc:
        return _error_response(500, sanitise(str(exc)[:500]))


def _parse_primer3_output(raw: str) -> list[dict]:
    """Parse Primer3 boulder-IO output into list of primer pair dicts."""
    lines = raw.strip().splitlines()
    kv: dict[str, str] = {}
    for line in lines:
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    primers: list[dict] = []
    i = 0
    while True:
        left_seq = kv.get(f"PRIMER_LEFT_{i}_SEQUENCE")
        right_seq = kv.get(f"PRIMER_RIGHT_{i}_SEQUENCE")
        if not left_seq:
            break

        left_pos = kv.get(f"PRIMER_LEFT_{i}", "")
        right_pos = kv.get(f"PRIMER_RIGHT_{i}", "")

        pair: dict = {
            "pair_index": i,
            "left_sequence": left_seq,
            "right_sequence": right_seq,
            "left_tm": _safe_float(kv.get(f"PRIMER_LEFT_{i}_TM")),
            "right_tm": _safe_float(kv.get(f"PRIMER_RIGHT_{i}_TM")),
            "left_gc": _safe_float(kv.get(f"PRIMER_LEFT_{i}_GC_PERCENT")),
            "right_gc": _safe_float(kv.get(f"PRIMER_RIGHT_{i}_GC_PERCENT")),
            "product_size": _safe_int(kv.get(f"PRIMER_PAIR_{i}_PRODUCT_SIZE")),
            "pair_penalty": _safe_float(kv.get(f"PRIMER_PAIR_{i}_PENALTY")),
        }
        if left_pos:
            parts = left_pos.split(",")
            pair["left_start"] = int(parts[0]) if parts else 0
            pair["left_length"] = int(parts[1]) if len(parts) > 1 else 0
        if right_pos:
            parts = right_pos.split(",")
            pair["right_start"] = int(parts[0]) if parts else 0
            pair["right_length"] = int(parts[1]) if len(parts) > 1 else 0

        primers.append(pair)
        i += 1

    return primers


def _safe_float(val: str | None) -> float | None:
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _safe_int(val: str | None) -> int | None:
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return None
