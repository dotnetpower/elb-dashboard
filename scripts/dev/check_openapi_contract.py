#!/usr/bin/env python3
"""Check the FastAPI OpenAPI contract against a reviewed structural baseline.

Responsibility: Extract stable operation/schema contracts, detect common breaking changes,
    and maintain the checked-in baseline used by CI and generated TypeScript types.
Edit boundaries: Build-time introspection only; never start servers, call Azure, or mutate app
    state. Runtime route behavior remains owned by ``api`` modules.
Key entry points: ``extract_contract``, ``breaking_changes``, ``main``.
Risky contracts: Additive operations/properties are compatible; removed operations/properties,
    operationId changes, narrower enums, and newly-required inputs are breaking. A baseline
    update must never silently authorize a breaking change without an explicit commit marker.
Validation: ``uv run pytest -q api/tests/test_openapi_contract.py`` and
    ``uv run python scripts/dev/check_openapi_contract.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
BASELINE_PATH = Path(__file__).with_name("openapi-contract-baseline.json")
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
IGNORED_SCHEMA_KEYS = frozenset({"title", "description", "example", "examples", "default"})


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize(item)
            for key, item in sorted(value.items())
            if key not in IGNORED_SCHEMA_KEYS
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _content_schemas(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    content = value.get("content")
    if not isinstance(content, dict):
        return {}
    return {
        media_type: _normalize(media.get("schema", {}))
        for media_type, media in sorted(content.items())
        if isinstance(media, dict)
    }


def extract_contract(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, documentation-free API compatibility contract."""
    operations: dict[str, Any] = {}
    paths = spec.get("paths") if isinstance(spec.get("paths"), dict) else {}
    for path, path_item in sorted(paths.items()):
        if not isinstance(path_item, dict):
            continue
        shared_parameters = path_item.get("parameters", [])
        for method, operation in sorted(path_item.items()):
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            parameters: dict[str, Any] = {}
            combined = [
                *(shared_parameters if isinstance(shared_parameters, list) else []),
                *(
                    operation.get("parameters", [])
                    if isinstance(operation.get("parameters"), list)
                    else []
                ),
            ]
            for parameter in combined:
                if not isinstance(parameter, dict):
                    continue
                location = str(parameter.get("in") or "")
                name = str(parameter.get("name") or "")
                if location and name:
                    parameters[f"{location}:{name}"] = {
                        "required": bool(parameter.get("required")),
                        "schema": _normalize(parameter.get("schema", {})),
                    }
            request_body = operation.get("requestBody")
            responses = operation.get("responses")
            operations[f"{method.upper()} {path}"] = {
                "operation_id": str(operation.get("operationId") or ""),
                "parameters": parameters,
                "request_body": {
                    "required": bool(request_body.get("required"))
                    if isinstance(request_body, dict)
                    else False,
                    "content": _content_schemas(request_body),
                },
                "responses": {
                    str(code): _content_schemas(response)
                    for code, response in sorted(
                        responses.items() if isinstance(responses, dict) else []
                    )
                },
                "security": _normalize(operation.get("security", [])),
            }
    components = spec.get("components") if isinstance(spec.get("components"), dict) else {}
    schemas = components.get("schemas") if isinstance(components.get("schemas"), dict) else {}
    return {
        "schema_version": 1,
        "global_security": _normalize(spec.get("security", [])),
        "operations": operations,
        "schemas": {name: _normalize(schema) for name, schema in sorted(schemas.items())},
    }


def _schema_breaks(old: Any, new: Any, location: str) -> list[str]:
    if not isinstance(old, dict) or not isinstance(new, dict):
        return [] if old == new else [f"schema changed: {location}"]
    changes: list[str] = []
    for key in ("type", "format", "$ref"):
        if key in old and old.get(key) != new.get(key):
            changes.append(f"schema {key} changed: {location}")
    old_enum = set(old.get("enum", [])) if isinstance(old.get("enum"), list) else set()
    new_enum = set(new.get("enum", [])) if isinstance(new.get("enum"), list) else set()
    if old_enum - new_enum:
        changes.append(f"enum values removed: {location}")
    for key in ("anyOf", "oneOf"):
        old_variants = old.get(key) if isinstance(old.get(key), list) else []
        new_variants = new.get(key) if isinstance(new.get(key), list) else []
        if old_variants:
            old_set = {json.dumps(_normalize(item), sort_keys=True) for item in old_variants}
            new_set = {json.dumps(_normalize(item), sort_keys=True) for item in new_variants}
            if old_set - new_set:
                changes.append(f"schema {key} variants removed or narrowed: {location}")
    old_all_of = old.get("allOf") if isinstance(old.get("allOf"), list) else []
    new_all_of = new.get("allOf") if isinstance(new.get("allOf"), list) else []
    if new_all_of:
        old_set = {json.dumps(_normalize(item), sort_keys=True) for item in old_all_of}
        new_set = {json.dumps(_normalize(item), sort_keys=True) for item in new_all_of}
        if new_set - old_set:
            changes.append(f"schema allOf constraints added or narrowed: {location}")
    lower_bounds = ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties")
    upper_bounds = ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties")
    for key in lower_bounds:
        old_value = old.get(key)
        new_value = new.get(key)
        if new_value is not None and (old_value is None or new_value > old_value):
            changes.append(f"schema {key} tightened: {location}")
    for key in upper_bounds:
        old_value = old.get(key)
        new_value = new.get(key)
        if new_value is not None and (old_value is None or new_value < old_value):
            changes.append(f"schema {key} tightened: {location}")
    for key in ("pattern", "multipleOf", "const"):
        if key in new and old.get(key) != new.get(key):
            changes.append(f"schema {key} added or changed: {location}")
    if old.get("nullable") is True and new.get("nullable") is not True:
        changes.append(f"schema nullable removed: {location}")
    if old.get("uniqueItems") is not True and new.get("uniqueItems") is True:
        changes.append(f"schema uniqueItems enabled: {location}")
    old_additional = old.get("additionalProperties", True)
    new_additional = new.get("additionalProperties", True)
    if old_additional is not False and new_additional is False:
        changes.append(f"schema additional properties disabled: {location}")
    elif isinstance(new_additional, dict):
        if old_additional is True:
            changes.append(f"schema additional properties constrained: {location}")
        elif isinstance(old_additional, dict):
            changes.extend(
                _schema_breaks(old_additional, new_additional, f"{location}{{}}")
            )
    old_required = set(old.get("required", [])) if isinstance(old.get("required"), list) else set()
    new_required = set(new.get("required", [])) if isinstance(new.get("required"), list) else set()
    if new_required - old_required:
        changes.append(f"required properties added: {location}")
    old_properties = old.get("properties") if isinstance(old.get("properties"), dict) else {}
    new_properties = new.get("properties") if isinstance(new.get("properties"), dict) else {}
    for name, old_property in old_properties.items():
        if name not in new_properties:
            changes.append(f"property removed: {location}.{name}")
            continue
        changes.extend(_schema_breaks(old_property, new_properties[name], f"{location}.{name}"))
    if "items" in old:
        changes.extend(_schema_breaks(old.get("items"), new.get("items"), f"{location}[]"))
    return changes


def breaking_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Describe backwards-incompatible changes between two extracted contracts."""
    changes: list[str] = []
    old_operations = old.get("operations", {})
    new_operations = new.get("operations", {})
    for key, old_operation in old_operations.items():
        new_operation = new_operations.get(key)
        if not isinstance(new_operation, dict):
            changes.append(f"operation removed: {key}")
            continue
        if old_operation.get("operation_id") != new_operation.get("operation_id"):
            changes.append(f"operationId changed: {key}")
        if old_operation.get("security", []) != new_operation.get("security", []):
            changes.append(f"operation security changed: {key}")
        old_parameters = old_operation.get("parameters", {})
        new_parameters = new_operation.get("parameters", {})
        for name, old_parameter in old_parameters.items():
            if name not in new_parameters:
                changes.append(f"parameter removed: {key} {name}")
            elif old_parameter != new_parameters[name]:
                changes.append(f"parameter changed: {key} {name}")
        for name, new_parameter in new_parameters.items():
            if name not in old_parameters and new_parameter.get("required"):
                changes.append(f"required parameter added: {key} {name}")
        old_body = old_operation.get("request_body", {})
        new_body = new_operation.get("request_body", {})
        if not old_body.get("required") and new_body.get("required"):
            changes.append(f"request body became required: {key}")
        for media_type, old_schema in old_body.get("content", {}).items():
            new_schema = new_body.get("content", {}).get(media_type)
            if new_schema is None:
                changes.append(f"request media type removed: {key} {media_type}")
            else:
                changes.extend(_schema_breaks(old_schema, new_schema, f"{key} request"))
        for status, old_response in old_operation.get("responses", {}).items():
            new_response = new_operation.get("responses", {}).get(status)
            if new_response is None:
                changes.append(f"response removed: {key} {status}")
                continue
            for media_type, old_schema in old_response.items():
                new_schema = new_response.get(media_type)
                if new_schema is None:
                    changes.append(f"response media type removed: {key} {status} {media_type}")
                else:
                    changes.extend(
                        _schema_breaks(old_schema, new_schema, f"{key} response {status}")
                    )

    old_schemas = old.get("schemas", {})
    new_schemas = new.get("schemas", {})
    for name, old_schema in old_schemas.items():
        if name not in new_schemas:
            changes.append(f"component schema removed: {name}")
        else:
            changes.extend(_schema_breaks(old_schema, new_schemas[name], f"component {name}"))
    if old.get("global_security", []) != new.get("global_security", []):
        changes.append("global security requirement changed")
    return sorted(set(changes))


def _current_spec() -> dict[str, Any]:
    os.environ.setdefault("AUTH_DEV_BYPASS", "true")
    os.environ.setdefault("ENABLE_DOCS", "true")
    from api.main import app

    return app.openapi()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def _baseline_at_ref(ref: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        raise ValueError("against-ref must be a Git commit SHA")
    relative = BASELINE_PATH.relative_to(REPO_ROOT).as_posix()
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git executable not found")
    result = subprocess.run(  # noqa: S603 - fixed executable + validated SHA argv.
        [git, "show", f"{ref}:{relative}"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = json.loads(result.stdout)
    return value if isinstance(value, dict) else None


def _has_breaking_marker(ref: str) -> bool:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        raise ValueError("against-ref must be a Git commit SHA")
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git executable not found")
    result = subprocess.run(  # noqa: S603 - fixed executable + validated SHA argv.
        [git, "log", "--format=%B", f"{ref}..HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(
        re.search(
            r"(?im)^(?:BREAKING CHANGE:|breaking:|[a-z]+(?:\([^)]*\))?!:)",
            result.stdout,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="Write the current contract baseline")
    parser.add_argument("--schema-output", type=Path, help="Write the full current OpenAPI JSON")
    parser.add_argument(
        "--against-ref",
        default="",
        help="Git commit SHA containing the previous baseline",
    )
    args = parser.parse_args(argv)

    spec = _current_spec()
    contract = extract_contract(spec)
    if args.schema_output:
        args.schema_output.parent.mkdir(parents=True, exist_ok=True)
        args.schema_output.write_text(
            json.dumps(spec, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.update:
        BASELINE_PATH.write_text(
            json.dumps(contract, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Updated {BASELINE_PATH} with {len(contract['operations'])} operations.")
        return 0
    if not BASELINE_PATH.exists():
        print(f"OpenAPI baseline is missing: {BASELINE_PATH}")
        return 1
    baseline = _load_json(BASELINE_PATH)
    if contract != baseline:
        print("OpenAPI contract differs from the checked-in baseline; review and run --update.")
        return 1
    if args.against_ref:
        previous = _baseline_at_ref(args.against_ref)
        if previous is not None:
            changes = breaking_changes(previous, baseline)
            if changes and not _has_breaking_marker(args.against_ref):
                print("Breaking OpenAPI changes require a breaking commit marker:")
                for change in changes:
                    print(f"  - {change}")
                return 1
    print(f"OpenAPI contract unchanged: {len(contract['operations'])} operations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
