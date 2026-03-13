from __future__ import annotations

from types import SimpleNamespace

from orchestrator.storage.vector_type import _PostgresVectorType, VectorJSONCompat, normalize_vector_values, vector_literal


def test_normalize_vector_values_accepts_pgvector_literal() -> None:
    assert normalize_vector_values("[1,2.5,3]") == [1.0, 2.5, 3.0]


def test_normalize_vector_values_accepts_json_string() -> None:
    assert normalize_vector_values("[1.25, 2, 3.5]") == [1.25, 2.0, 3.5]


def test_vector_json_compat_postgres_bind_returns_literal_once() -> None:
    vector_type = VectorJSONCompat(3)
    dialect = SimpleNamespace(name="postgresql")

    bound = vector_type.process_bind_param("[1,2,3]", dialect)

    impl = _PostgresVectorType(3)
    processor = impl.bind_processor(dialect)
    assert processor is not None
    assert processor(bound) == vector_literal([1.0, 2.0, 3.0])
