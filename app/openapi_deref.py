"""
OpenAPI JSON dereferencer for MCPHub compatibility.

MCPHub's OpenAPI parser doesn't resolve $ref pointers in path-level schemas
(requestBody / responses). This module walks the generated spec and inlines
all component references so the resulting JSON contains zero $ref keys.
"""

import copy
from typing import Any


def _resolve_ref(ref: str, components: dict[str, Any]) -> Any:
    """Resolve a #/components/schemas/... reference."""
    if not ref.startswith("#/components/schemas/"):
        raise ValueError(f"Unsupported $ref: {ref}")
    parts = ref.replace("#/components/schemas/", "").split("/")
    target = components
    for part in parts:
        if not isinstance(target, dict) or part not in target:
            raise ValueError(f"Cannot resolve $ref: {ref}")
        target = target[part]
    return copy.deepcopy(target)


def _inline_dict(obj: Any, components: dict[str, Any]) -> Any:
    """Recursively replace $ref with inlined schemas."""
    if isinstance(obj, dict):
        if "$$ref" in obj:
            # openapi-generator / swagger-codegen marker
            del obj["$$ref"]
        if "$ref" in obj:
            inlined = _resolve_ref(obj["$ref"], components)
            # Preserve any sibling keys (OpenAPI 3.1 allows them alongside $ref)
            new_obj = copy.deepcopy(inlined)
            if isinstance(new_obj, dict):
                for k, v in obj.items():
                    if k not in {"$ref", "$$ref"}:
                        new_obj[k] = copy.deepcopy(v)
            return _inline_dict(new_obj, components)
        return {k: _inline_dict(v, components) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_inline_dict(item, components) for item in obj]
    return obj


def dereference(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a new spec with all $ref references fully inlined."""
    result = copy.deepcopy(spec)
    components = result.get("components", {}).get("schemas", {})
    if not components:
        return result
    return _inline_dict(result, components)
