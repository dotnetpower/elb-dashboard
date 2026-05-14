"""Tests for ProvisionTerminalRequest validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.terminal import ProvisionTerminalRequest


def _payload(**overrides: object) -> dict[str, object]:
    base = {
        "subscription_id": "00000000-0000-0000-0000-000000000000",
        "allowed_ssh_cidr": "203.0.113.5/32",
    }
    base.update(overrides)
    return base


def test_defaults_apply_when_optional_fields_missing() -> None:
    req = ProvisionTerminalRequest.model_validate(_payload())
    assert req.resource_group == "rg-elb-terminal"
    assert req.region == "koreacentral"
    assert req.vm_name == "vm-elb-terminal"
    assert req.vm_size == "Standard_D4s_v5"
    assert req.admin_username == "azureuser"


def test_subscription_id_required() -> None:
    with pytest.raises(ValidationError):
        ProvisionTerminalRequest.model_validate({"allowed_ssh_cidr": "1.2.3.4/32"})


def test_vm_name_length_capped_at_15() -> None:
    with pytest.raises(ValidationError):
        ProvisionTerminalRequest.model_validate(_payload(vm_name="this-name-is-way-too-long"))


def test_overrides_round_trip() -> None:
    req = ProvisionTerminalRequest.model_validate(
        _payload(
            resource_group="rg-custom",
            region="eastus",
            vm_name="vm-1",
            vm_size="Standard_D2s_v5",
            admin_username="ops",
        )
    )
    assert req.resource_group == "rg-custom"
    assert req.region == "eastus"
    assert req.vm_name == "vm-1"
    assert req.vm_size == "Standard_D2s_v5"
    assert req.admin_username == "ops"
