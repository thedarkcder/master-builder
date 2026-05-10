from __future__ import annotations

# Storage model modules import the shared SQLAlchemy symbols from this base module.
# ruff: noqa: F401
from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from orchestrator.storage.vector_type import VectorJSONCompat


class Base(DeclarativeBase):
    pass
