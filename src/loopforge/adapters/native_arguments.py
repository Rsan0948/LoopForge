"""Declared provider structures normalized into strict string-valued runtime actions."""

import json
from collections.abc import Mapping
from typing import cast

from loopforge.ports.model import ModelToolSpec


def native_parameters(spec: ModelToolSpec) -> dict[str, object]:
    schema = dict(spec.parameters)
    properties = schema.get("properties")
    if spec.json_text_parameters and isinstance(properties, dict):
        fields = dict(cast("dict[str, object]", properties))
        for name, codec in spec.json_text_parameters.items():
            fields[name] = codec.schema
        schema["properties"] = fields
    return schema


def native_arguments(name: str, arguments: object, tools: tuple[ModelToolSpec, ...]) -> object:
    spec = next((s for s in tools if s.name == name), None)
    if spec is None or not spec.json_text_parameters or not isinstance(arguments, dict):
        return arguments
    result = dict(cast("dict[str, object]", arguments))
    for field, codec in spec.json_text_parameters.items():
        if field in result:
            result[field] = codec.encode(result[field])
    return result


def normalize_chat(data: object, tools: tuple[ModelToolSpec, ...]) -> object:
    if not isinstance(data, Mapping):
        return data
    result = dict(cast("Mapping[str, object]", data))
    message = result.get("message")
    if not isinstance(message, dict):
        return cast("object", data)
    message = dict(cast("dict[str, object]", message))
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return cast("object", data)
    normalized: list[object] = []
    for call in cast("list[object]", calls):
        if not isinstance(call, dict):
            normalized.append(call)
            continue
        item = dict(cast("dict[str, object]", call))
        fn = item.get("function")
        if isinstance(fn, dict):
            function = dict(cast("dict[str, object]", fn))
            name = function.get("name")
            if isinstance(name, str):
                function["arguments"] = native_arguments(name, function.get("arguments"), tools)
            item["function"] = function
        normalized.append(item)
    message["tool_calls"] = normalized
    result["message"] = message
    return result


def wire_arguments(
    name: str, arguments: dict[str, str], tools: tuple[ModelToolSpec, ...]
) -> dict[str, object]:
    """Keep assistant tool-call history in the same schema the provider sees."""
    spec = next((s for s in tools if s.name == name), None)
    result: dict[str, object] = dict(arguments)
    if spec is not None:
        for field, codec in spec.json_text_parameters.items():
            if field in arguments:
                decoded: object = json.loads(arguments[field])
                codec.encode(decoded)  # Validate before placing it on the wire again.
                result[field] = decoded
    return result
