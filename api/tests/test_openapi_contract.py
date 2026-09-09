"""Tests for the build-time OpenAPI compatibility contract.

Responsibility: Verify deterministic extraction and breaking/additive classification without
    modifying runtime routes or using Azure resources.
Edit boundaries: Pure contract fixtures plus one current-app uniqueness assertion.
Key entry points: ``test_*``.
Risky contracts: Additive operations and optional properties must remain allowed; removals,
    operationId changes, narrower enums, and newly-required inputs must fail.
Validation: ``uv run pytest -q api/tests/test_openapi_contract.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "dev" / "check_openapi_contract.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("check_openapi_contract", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spec(*, required: list[str] | None = None, enum: list[str] | None = None) -> dict:
    return {
        "security": [{"BearerAuth": []}],
        "paths": {
            "/api/items": {
                "post": {
                    "operationId": "create_item",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
                        },
                    },
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Item"}
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Item": {
                    "type": "object",
                    "required": required or ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "mode": {"type": "string", "enum": enum or ["a", "b"]},
                    },
                }
            }
        },
    }


def test_contract_extraction_ignores_documentation_text() -> None:
    module = _module()
    first = _spec()
    second = _spec()
    first["components"]["schemas"]["Item"]["description"] = "before"
    second["components"]["schemas"]["Item"]["description"] = "after"

    assert module.extract_contract(first) == module.extract_contract(second)


def test_breaking_changes_detect_contract_narrowing() -> None:
    module = _module()
    old = module.extract_contract(_spec())
    new_spec = _spec(required=["name", "mode"], enum=["a"])
    new_spec["paths"]["/api/items"]["post"]["operationId"] = "renamed"
    new = module.extract_contract(new_spec)

    changes = module.breaking_changes(old, new)

    assert any("operationId changed" in change for change in changes)
    assert any("required properties added" in change for change in changes)
    assert any("enum values removed" in change for change in changes)


def test_additive_operation_and_optional_property_are_compatible() -> None:
    module = _module()
    old = module.extract_contract(_spec())
    new_spec = _spec()
    new_spec["components"]["schemas"]["Item"]["properties"]["note"] = {"type": "string"}
    new_spec["paths"]["/api/items/{item_id}"] = {
        "get": {"operationId": "get_item", "responses": {"200": {}}}
    }

    assert module.breaking_changes(old, module.extract_contract(new_spec)) == []


def test_current_app_operation_ids_are_present_and_unique(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    module = _module()
    contract = module.extract_contract(module._current_spec())
    operation_ids = [value["operation_id"] for value in contract["operations"].values()]

    assert len(operation_ids) > 100
    assert all(operation_ids)
    assert len(operation_ids) == len(set(operation_ids))
