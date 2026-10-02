"""Durable anonymous admission budgets; raw identity values are never stored."""

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class AuthRequestBudget(Base):
    __tablename__ = "auth_request_budgets"
    __table_args__ = (
        CheckConstraint("attempts > 0", name="ck_auth_budget_positive"),
        Index("ix_auth_request_budgets_expires_at", "expires_at"),
    )
    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
