#!/usr/bin/env python3
"""Convert a Bicep-compiled ARM template to portal-friendly language version 1.x shape."""

from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from typing import Any

_REFERENCE_PATTERN = re.compile(r"reference\('([A-Za-z_][A-Za-z0-9_]*)'\)")


def _resource_id_expr(resource_type: str, name: Any) -> str:
    if isinstance(name, str) and name.startswith("["):
        inner = name[1:-1]
        return f"resourceId('{resource_type}', {inner})"
    return f"resourceId('{resource_type}', '{name}')"


def _replace_symbolic_references(value: Any, symbols: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, list):
        return [_replace_symbolic_references(item, symbols) for item in value]
    if isinstance(value, dict):
        return {key: _replace_symbolic_references(item, symbols) for key, item in value.items()}
    if isinstance(value, str) and value.startswith("[") and value.endswith("]"):
        inner = value[1:-1]

        def replacer(match: re.Match[str]) -> str:
            symbol = match.group(1)
            resource = symbols.get(symbol)
            if resource is None:
                return match.group(0)
            resource_id = _resource_id_expr(resource["type"], resource["name"])
            api_version = resource["apiVersion"]
            return f"reference({resource_id}, '{api_version}')"

        replaced = _REFERENCE_PATTERN.sub(replacer, inner)
        if replaced == inner:
            return value
        return f"[{replaced}]"
    return value


def _portalize_template_object(template: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(template)
    result.pop("languageVersion", None)

    resources = result.get("resources")
    symbols: dict[str, dict[str, Any]] = {}
    if isinstance(resources, dict):
        symbols = {name: resource for name, resource in resources.items()}
        for resource in symbols.values():
            depends_on = resource.get("dependsOn")
            if not depends_on:
                continue
            new_depends_on: list[str] = []
            for dep in depends_on:
                if dep in symbols:
                    dep_resource = symbols[dep]
                    expr = _resource_id_expr(dep_resource["type"], dep_resource["name"])
                    new_depends_on.append(f"[{expr}]")
                else:
                    new_depends_on.append(dep)
            resource["dependsOn"] = new_depends_on
        result["resources"] = list(symbols.values())

    if symbols:
        result = _replace_symbolic_references(result, symbols)

    resources_list = result.get("resources")
    if isinstance(resources_list, list):
        new_resources: list[Any] = []
        for resource in resources_list:
            if isinstance(resource, dict):
                properties = resource.get("properties")
                if isinstance(properties, dict):
                    nested = properties.get("template")
                    if isinstance(nested, dict):
                        resource = deepcopy(resource)
                        resource["properties"]["template"] = _portalize_template_object(
                            nested
                        )
            new_resources.append(resource)
        result["resources"] = new_resources

    return result


def portalize_template(template: dict[str, Any]) -> dict[str, Any]:
    return _portalize_template_object(template)


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <input.json> <output.json>", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as handle:
        template = json.load(handle)
    output = portalize_template(template)
    with open(sys.argv[2], "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())