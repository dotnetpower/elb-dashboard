"""Pydantic models for BLAST job submission, status, and results."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

_SHELL_METACHAR = re.compile(r"[;&|`$(){}\\!\n\r<>~\[\]?*]")


class BlastProgram(StrEnum):
    BLASTN = "blastn"
    BLASTP = "blastp"
    BLASTX = "blastx"
    TBLASTN = "tblastn"
    TBLASTX = "tblastx"
    PSIBLAST = "psiblast"
    RPSBLAST = "rpsblast"
    RPSTBLASTN = "rpstblastn"


class BlastJobPhase(StrEnum):
    UPLOADING = "uploading"
    CONFIGURING = "configuring"
    ENABLING_STORAGE = "enabling_storage"
    SUBMITTING = "submitting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class BlastSubmitRequest(BaseModel):
    """User input for a BLAST job submission."""

    subscription_id: str
    resource_group: str = Field(..., min_length=1, max_length=90)
    region: str = Field("koreacentral", min_length=2, max_length=40)

    # BLAST parameters
    program: BlastProgram
    db: str = Field(..., description="Database path in storage, e.g. blast-db/pdbnt/pdbnt")
    query_data: str | None = Field(None, description="FASTA sequence text (inline)")
    query_blob_url: str | None = Field(None, description="Pre-uploaded query blob URL")
    job_title: str = Field("", max_length=200)

    # Algorithm parameters
    evalue: float = Field(10.0, gt=0)
    max_target_seqs: int = Field(500, ge=1, le=50000)
    outfmt: int = Field(7, ge=0, le=18)
    word_size: int | None = Field(None, ge=2)
    gap_open: int | None = None
    gap_extend: int | None = None
    additional_options: str = Field("", max_length=500)

    # #23: Reject shell metacharacters at the Pydantic boundary
    @field_validator("additional_options")
    @classmethod
    def reject_shell_metachar(cls, v: str) -> str:
        if _SHELL_METACHAR.search(v):
            raise ValueError("additional_options contains forbidden characters")
        return v

    # Cluster configuration
    machine_type: str = Field("Standard_E32s_v3")
    num_nodes: int = Field(1, ge=1, le=100)
    pd_size: str = Field("3000Gi")
    mem_request: str = Field("8Gi")
    mem_limit: str = Field("24Gi")
    batch_len: int | None = None

    # Warm cluster / DB sharding
    enable_warmup: bool = Field(True, description="Run prepare step to warm cluster with DB shards before BLAST")
    reuse: bool = Field(False, description="Reuse existing warm cluster instead of creating a new one")
    db_auto_partition: bool = Field(True, description="Automatically partition DB into shards for parallel search")
    db_partitions: int = Field(0, ge=0, le=64, description="Number of DB partitions (0 = auto)")
    db_partition_prefix: str = Field("", max_length=200)

    # Azure resource names
    acr_resource_group: str = Field("")
    acr_name: str = Field("")
    storage_account: str = Field("")

    # Terminal VM (for elastic-blast CLI execution)
    terminal_resource_group: str = Field("rg-elb-terminal")
    terminal_vm_name: str = Field("vm-elb-terminal")

    @model_validator(mode="after")
    def check_query_source(self) -> BlastSubmitRequest:
        has_data = bool(self.query_data)
        has_url = bool(self.query_blob_url)
        if not has_data and not has_url:
            raise ValueError("provide either query_data or query_blob_url")
        return self


class BlastJobSummary(BaseModel):
    """Stored per-job metadata."""

    job_id: str
    job_title: str = ""
    program: str
    db: str
    query_blob_url: str = ""
    status: str = "submitted"
    phase: str = BlastJobPhase.UPLOADING.value
    created_at: str = ""
    updated_at: str = ""
    elapsed_seconds: float | None = None
    results_url: str = ""
    error: str = ""
    config_snapshot: dict[str, Any] = Field(default_factory=dict)


class UploadQueryRequest(BaseModel):
    subscription_id: str
    resource_group: str
    storage_account: str
    container: str = Field("queries")
    filename: str = Field("input.fa")


class BlastResultFile(BaseModel):
    name: str
    size: int | None = None
    last_modified: str | None = None
    download_url: str | None = None
