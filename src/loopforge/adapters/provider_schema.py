"""Preserve single-value constraints for providers that support enum but not const."""

from collections.abc import Mapping


def enum_from_const(schema: Mapping[str, object]) -> dict[str, object]:
    result = dict(schema)
    if "const" not in result:
        return result
    value = result.pop("const")
    allowed = result.get("enum")
    if allowed is not None and (not isinstance(allowed, list) or value not in allowed):
        msg = "Conflicting const and enum in provider schema"
        raise ValueError(msg)
    result["enum"] = [value]
    return result
