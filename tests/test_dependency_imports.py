from typing_extensions import Annotated

from sqlalchemy import select


def test_runtime_dependency_imports_are_available() -> None:
    assert Annotated is not None
    assert select(1) is not None
