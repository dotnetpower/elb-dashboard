"""Pydantic models shared across HTTP triggers and orchestrators."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProvisionTerminalRequest(BaseModel):
    """User-overridable inputs when provisioning a Remote Terminal VM."""

    subscription_id: str = Field(..., description="Subscription that will host the VM.")
    resource_group: str = Field("rg-elb-terminal", min_length=1, max_length=90)
    region: str = Field("koreacentral", min_length=2, max_length=40)
    vm_name: str = Field("vm-elb-terminal", min_length=1, max_length=15)
    vm_size: str = Field("Standard_D4s_v5")
    admin_username: str = Field("azureuser", min_length=3, max_length=32)
    allowed_ssh_cidr: str = Field(
        ..., description="Caller egress IP in CIDR form, e.g. 1.2.3.4/32."
    )
    # Optional — for auto-assigning RBAC roles to VM managed identity
    workload_resource_group: str = Field("", description="Workload RG for Contributor role.")
    acr_resource_group: str = Field("", description="ACR RG for AcrPull role.")
    acr_name: str = Field("", description="ACR name for AcrPull role.")
    storage_account: str = Field("", description="Storage account for Blob Data Contributor role.")
    storage_resource_group: str = Field("", description="Storage account RG.")


class ProvisionTerminalStarted(BaseModel):
    instance_id: str
    status_query_uri: str


class TerminalConnectionInfo(BaseModel):
    vm_name: str
    resource_group: str
    region: str
    fqdn: str
    ssh_host: str
    ssh_port: int = 22
    username: str
    password_secret_uri: str
    cloud_init_status: Literal["running", "done", "failed", "unknown"] = "unknown"


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str = "0.1.0"
