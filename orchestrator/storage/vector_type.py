from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator, UserDefinedType


def vector_literal(values: Sequence[float] | None) -> str | None:
    if values is None:
        return None
    normalized = [float(value) for value in values]
    return "[" + ",".join(f"{value:.12g}" for value in normalized) + "]"


def parse_vector_value(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [float(item) for item in value]
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [float(item.strip()) for item in inner.split(",") if item.strip()]
    return None


class _PostgresVectorType(UserDefinedType):
    cache_ok = True

    def __init__(self, dimensions: int):
        self.dimensions = int(dimensions)

    def get_col_spec(self, **_kw: object) -> str:
        return f"vector({self.dimensions})"

    def bind_processor(self, _dialect):  # noqa: ANN001
        def _process(value: Sequence[float] | None) -> str | None:
            return vector_literal(value)

        return _process

    def result_processor(self, _dialect, _coltype):  # noqa: ANN001
        def _process(value: Any) -> list[float] | None:
            return parse_vector_value(value)

        return _process


class VectorJSONCompat(TypeDecorator):
    impl = JSON
    cache_ok = True

    def __init__(self, dimensions: int):
        super().__init__()
        self.dimensions = int(dimensions)

    def load_dialect_impl(self, dialect):  # noqa: ANN001
        if dialect.name == "postgresql":
            return dialect.type_descriptor(_PostgresVectorType(self.dimensions))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: Sequence[float] | None, dialect):  # noqa: ANN001
        if value is None:
            return None
        normalized = [float(item) for item in value]
        if dialect.name == "postgresql":
            return vector_literal(normalized)
        return normalized

    def process_result_value(self, value: Any, _dialect) -> list[float] | None:
        return parse_vector_value(value)

