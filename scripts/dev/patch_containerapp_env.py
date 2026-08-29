#!/usr/bin/env python3
"""Build a Container App template patch for one exact container.

Responsibility: Convert a full Container App ARM snapshot plus desired image,
    resources, and KEY=VALUE inputs into one template-only patch for a named
    container.
Edit boundaries: Pure JSON transformation and CLI file I/O only; Azure reads,
    REST writes, revision polling, and deployment sequencing stay in
    `scripts/dev/quick-deploy.sh`.
Key entry points: `build_template_patch`, `parse_env_pair`, `main`.
Risky contracts: Preserve every container and template field, distinguish plain
    values from `secretref:` references, reject duplicate keys or missing
    containers, and report unchanged input without creating a revision.
Validation: `uv run pytest -q api/tests/test_patch_containerapp_env.py`.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MEMORY_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti)?$")
_MEMORY_FACTORS = {
    None: Decimal(1),
    "Ki": Decimal(1024),
    "Mi": Decimal(1024**2),
    "Gi": Decimal(1024**3),
    "Ti": Decimal(1024**4),
}


def _memory_bytes(value: object) -> Decimal | None:
    """Normalize a Container Apps memory quantity for semantic comparison."""
    if not isinstance(value, str):
        return None
    match = _MEMORY_RE.fullmatch(value)
    if match is None:
        return None
    try:
        return Decimal(match.group(1)) * _MEMORY_FACTORS[match.group(2)]
    except (InvalidOperation, KeyError):
        return None


def _memory_equal(current: object, desired: str) -> bool:
    current_bytes = _memory_bytes(current)
    desired_bytes = _memory_bytes(desired)
    if current_bytes is not None and desired_bytes is not None:
        return current_bytes == desired_bytes
    return current == desired


def parse_env_pair(pair: str) -> tuple[str, str, bool]:
    """Return `(name, value, is_secret_ref)` for one CLI environment pair."""
    name, separator, value = pair.partition("=")
    if not separator or not _ENV_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid environment pair: {pair!r}")
    if value.startswith("secretref:"):
        secret_name = value.removeprefix("secretref:")
        if not secret_name:
            raise ValueError(f"empty secret reference for {name}")
        return name, secret_name, True
    return name, value, False


def build_template_patch(
    resource: dict[str, Any],
    *,
    container_name: str,
    env_pairs: list[str],
    revision_suffix: str,
    image: str | None = None,
    cpu: str | None = None,
    memory: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Build a template-only ARM patch and report whether values changed."""
    if not revision_suffix:
        raise ValueError("revision_suffix is required")
    template = copy.deepcopy(resource.get("properties", {}).get("template"))
    if not isinstance(template, dict):
        raise ValueError("Container App snapshot has no properties.template object")
    containers = template.get("containers")
    if not isinstance(containers, list):
        raise ValueError("Container App template has no containers array")
    matches = [
        container
        for container in containers
        if isinstance(container, dict) and container.get("name") == container_name
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one container named {container_name!r}, found {len(matches)}"
        )

    parsed: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for pair in env_pairs:
        item = parse_env_pair(pair)
        if item[0] in seen:
            raise ValueError(f"duplicate environment key: {item[0]}")
        seen.add(item[0])
        parsed.append(item)

    container = matches[0]
    changed = False
    if image is not None and container.get("image") != image:
        container["image"] = image
        changed = True
    if cpu is not None or memory is not None:
        resources = container.get("resources")
        if resources is None:
            resources = {}
        if not isinstance(resources, dict):
            raise ValueError(f"container {container_name!r} has invalid resources")
        if cpu is not None:
            desired_cpu = float(cpu)
            if float(resources.get("cpu") or 0) != desired_cpu:
                resources["cpu"] = desired_cpu
                changed = True
        if memory is not None and not _memory_equal(resources.get("memory"), memory):
            resources["memory"] = memory
            changed = True
        container["resources"] = resources

    raw_env = container.get("env")
    if raw_env is None:
        raw_env = []
    if not isinstance(raw_env, list) or not all(isinstance(item, dict) for item in raw_env):
        raise ValueError(f"container {container_name!r} has an invalid env array")
    env = list(raw_env)
    for name, value, is_secret_ref in parsed:
        desired = {"name": name, "secretRef" if is_secret_ref else "value": value}
        existing = [item for item in env if item.get("name") == name]
        if len(existing) == 1:
            record = existing[0]
            if is_secret_ref:
                env_matches = record.get("secretRef") == value and not record.get("value")
            else:
                env_matches = record.get("value") == value and not record.get("secretRef")
            if env_matches:
                continue
        env = [item for item in env if item.get("name") != name]
        env.append(desired)
        changed = True

    if changed:
        container["env"] = env
        template["revisionSuffix"] = revision_suffix
    return {"properties": {"template": template}}, changed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--revision-suffix", required=True)
    parser.add_argument("--image")
    parser.add_argument("--cpu")
    parser.add_argument("--memory")
    parser.add_argument("--env", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    resource = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(resource, dict):
        raise ValueError("Container App snapshot must be a JSON object")
    patch, changed = build_template_patch(
        resource,
        container_name=args.container,
        env_pairs=args.env,
        revision_suffix=args.revision_suffix,
        image=args.image,
        cpu=args.cpu,
        memory=args.memory,
    )
    args.output.write_text(json.dumps(patch, separators=(",", ":")), encoding="utf-8")
    print("changed" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
