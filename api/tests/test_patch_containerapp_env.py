"""Tests for exact-container Container App environment patch generation.

Responsibility: Verify pure ARM snapshot transformation for quick-deploy
    environment convergence without calling Azure.
Edit boundaries: JSON fixtures and assertions only; shell sequencing and live
    revision polling remain covered by deployment-script contract tests.
Key entry points: Tests for target isolation, plain and secret values,
    idempotent no-op, and malformed input rejection.
Risky contracts: A patch must retain every container/template field and must
    never apply one sidecar's environment to the default API container.
Validation: `uv run pytest -q api/tests/test_patch_containerapp_env.py`.
"""

from __future__ import annotations

import copy

import pytest
from scripts.dev.patch_containerapp_env import build_template_patch


def _resource() -> dict:
    return {
        "id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/containerApps/ca",
        "etag": '"etag-1"',
        "properties": {
            "template": {
                "revisionSuffix": "old-revision",
                "scale": {"minReplicas": 1, "maxReplicas": 1},
                "containers": [
                    {
                        "name": "api",
                        "image": "acr/elb-api:old",
                        "resources": {"cpu": 1.0, "memory": "2.0Gi"},
                        "env": [{"name": "API_ONLY", "value": "keep"}],
                    },
                    {
                        "name": "worker",
                        "image": "acr/elb-api:old",
                        "resources": {"cpu": 1.0, "memory": "2.0Gi"},
                        "env": [{"name": "KEEP", "value": "worker"}],
                    },
                    {
                        "name": "frontend",
                        "image": "acr/elb-frontend:old",
                        "resources": {"cpu": 0.5, "memory": "1.0Gi"},
                        "env": [],
                    },
                ],
            }
        },
    }


def test_patch_updates_only_named_container_and_preserves_template() -> None:
    resource = _resource()
    original = copy.deepcopy(resource)

    patch, changed = build_template_patch(
        resource,
        container_name="worker",
        env_pairs=["PLATFORM_ACR_NAME=acrelb", "EXEC_TOKEN=secretref:exec-token"],
        revision_suffix="env-worker-123",
        image="acr/elb-api:new",
        cpu="1.75",
        memory="3.5Gi",
    )

    assert changed is True
    template = patch["properties"]["template"]
    assert template["revisionSuffix"] == "env-worker-123"
    assert template["scale"] == original["properties"]["template"]["scale"]
    assert [item["name"] for item in template["containers"]] == [
        "api",
        "worker",
        "frontend",
    ]
    assert template["containers"][0] == original["properties"]["template"]["containers"][0]
    assert template["containers"][2] == original["properties"]["template"]["containers"][2]
    assert template["containers"][1]["image"] == "acr/elb-api:new"
    assert template["containers"][1]["resources"] == {"cpu": 1.75, "memory": "3.5Gi"}
    assert template["containers"][1]["env"] == [
        {"name": "KEEP", "value": "worker"},
        {"name": "PLATFORM_ACR_NAME", "value": "acrelb"},
        {"name": "EXEC_TOKEN", "secretRef": "exec-token"},
    ]
    assert resource == original


def test_patch_is_noop_when_exact_value_already_exists() -> None:
    resource = _resource()
    resource["properties"]["template"]["containers"][1]["env"].append(
        {"name": "PLATFORM_ACR_NAME", "value": "acrelb"}
    )

    patch, changed = build_template_patch(
        resource,
        container_name="worker",
        env_pairs=["PLATFORM_ACR_NAME=acrelb"],
        revision_suffix="must-not-land",
        image="acr/elb-api:old",
        cpu="1.0",
        memory="2.0Gi",
    )

    assert changed is False
    assert patch["properties"]["template"]["revisionSuffix"] == "old-revision"


def test_patch_treats_arm_null_opposite_field_as_unchanged() -> None:
    resource = _resource()
    resource["properties"]["template"]["containers"][1]["env"].append(
        {
            "name": "EXEC_TOKEN",
            "value": None,
            "secretRef": "exec-token",
        }
    )

    _patch, changed = build_template_patch(
        resource,
        container_name="worker",
        env_pairs=["EXEC_TOKEN=secretref:exec-token"],
        revision_suffix="must-not-land",
    )

    assert changed is False


def test_patch_replaces_plain_value_with_secret_reference() -> None:
    resource = _resource()
    resource["properties"]["template"]["containers"][1]["env"].append(
        {"name": "EXEC_TOKEN", "value": "stale-plain-value"}
    )

    patch, changed = build_template_patch(
        resource,
        container_name="worker",
        env_pairs=["EXEC_TOKEN=secretref:exec-token"],
        revision_suffix="env-worker-secret",
    )

    assert changed is True
    record = next(
        item
        for item in patch["properties"]["template"]["containers"][1]["env"]
        if item["name"] == "EXEC_TOKEN"
    )
    assert record == {"name": "EXEC_TOKEN", "secretRef": "exec-token"}


def test_patch_rejects_duplicate_keys_and_missing_container() -> None:
    with pytest.raises(ValueError, match="duplicate environment key"):
        build_template_patch(
            _resource(),
            container_name="worker",
            env_pairs=["KEY=one", "KEY=two"],
            revision_suffix="env-worker-duplicate",
        )

    with pytest.raises(ValueError, match="exactly one container"):
        build_template_patch(
            _resource(),
            container_name="beat",
            env_pairs=["KEY=value"],
            revision_suffix="env-beat-missing",
        )
