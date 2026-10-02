from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from types import NoneType, UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints


def prompt_model_from_dataclass(model_type: type) -> dict[str, object]:
    if not is_dataclass(model_type):
        raise TypeError(f"{model_type.__name__} must be a dataclass prompt contract")

    model_name = str(getattr(model_type, "__prompt_model_name__", model_type.__name__))
    include_fields = tuple(getattr(model_type, "__prompt_include_fields__", ()))
    aliases: dict[str, str] = dict(getattr(model_type, "__prompt_field_aliases__", {}))
    enums: dict[str, tuple[str, ...]] = dict(
        getattr(model_type, "__prompt_field_enums__", {})
    )
    nested_types: dict[str, type] = dict(
        getattr(model_type, "__prompt_nested_types__", {})
    )
    required_overrides: dict[str, bool] = dict(
        getattr(model_type, "__prompt_field_required__", {})
    )
    type_hints = get_type_hints(model_type)
    dataclass_fields = fields(model_type)
    field_names = include_fields or tuple(field.name for field in dataclass_fields)
    known_field_names = {field.name for field in dataclass_fields}

    rendered_fields: list[dict[str, object]] = []
    for field_name in field_names:
        if field_name not in known_field_names:
            raise TypeError(
                f"{model_type.__name__} prompt field {field_name!r} is not a dataclass field"
            )
        annotation = nested_types.get(field_name, type_hints.get(field_name, Any))
        required = required_overrides.get(field_name)
        rendered_field = _prompt_field(
            name=aliases.get(field_name, field_name),
            annotation=annotation,
            required=required,
            enum=enums.get(field_name),
        )
        rendered_fields.append(rendered_field)

    return {"name": model_name, "fields": rendered_fields}


def _prompt_field(
    *,
    name: str,
    annotation: object,
    required: bool | None,
    enum: tuple[str, ...] | None,
) -> dict[str, object]:
    optional, inner_annotation = _unwrap_optional(annotation)
    kind, fields_payload, literal_enum = _prompt_kind(inner_annotation)
    payload: dict[str, object] = {
        "name": name,
        "kind": kind,
        "required": (not optional) if required is None else required,
    }
    enum_values = enum or literal_enum
    if enum_values:
        payload["enum"] = list(enum_values)
    if fields_payload:
        payload["fields"] = fields_payload
    return payload


def _unwrap_optional(annotation: object) -> tuple[bool, object]:
    origin = get_origin(annotation)
    if origin in {Union, UnionType}:
        args = tuple(arg for arg in get_args(annotation))
        if NoneType in args:
            non_none_args = tuple(arg for arg in args if arg is not NoneType)
            return True, non_none_args[0] if len(non_none_args) == 1 else Union[
                non_none_args
            ]  # type: ignore[index]
    return False, annotation


def _prompt_kind(
    annotation: object,
) -> tuple[str, list[dict[str, object]] | None, tuple[str, ...]]:
    origin = get_origin(annotation)
    if origin is Literal:
        literal_values = tuple(str(value) for value in get_args(annotation))
        return "string", None, literal_values
    if origin in {tuple, list}:
        args = get_args(annotation)
        item_annotation = args[0] if args else Any
        item_kind, item_fields, item_enum = _prompt_kind(item_annotation)
        payload_kind = f"array<{item_kind}>"
        if item_kind == "object" and item_fields:
            return "array<object>", item_fields, item_enum
        return payload_kind, None, item_enum
    if origin is dict:
        return "object", None, ()
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "string", None, tuple(str(item.value) for item in annotation)
    if annotation is str:
        return "string", None, ()
    if annotation is bool:
        return "boolean", None, ()
    if annotation is int:
        return "integer", None, ()
    if annotation is float:
        return "number", None, ()
    if annotation is Any:
        return "object", None, ()
    if isinstance(annotation, type) and is_dataclass(annotation):
        return "object", list(prompt_model_from_dataclass(annotation)["fields"]), ()
    return "object", None, ()
