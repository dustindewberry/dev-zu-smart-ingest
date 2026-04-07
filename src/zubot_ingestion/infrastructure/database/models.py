"""SQLAlchemy 2.0 ORM models for zubot-ingestion (CAP-004).

These models define the persistent storage schema for batches, jobs, and
review actions. They mirror the dataclass entities in
zubot_ingestion.domain.entities but live in the infrastructure layer to
keep the domain layer free of ORM concerns.

Schema notes:
- All primary keys are server-generated UUIDs (uuid_generate_v4()).
- Job.result and Job.pipeline_trace use PostgreSQL JSONB.
- ReviewAction.corrections uses PostgreSQL JSONB.
- created_at / updated_at default to NOW() at insert time; updated_at is
  maintained by a database trigger (see migrations/versions/0001_initial.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import CHAR, TypeDecorator


class UUIDType(TypeDecorator):
    """Portable UUID column: native PG_UUID on PostgreSQL, CHAR(36) elsewhere.

    This lets the same metadata run under SQLite (tests) and PostgreSQL
    (production) without needing two parallel schemas.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[override]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):  # type: ignore[override]
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):  # type: ignore[override]
        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        return UUID(str(value))


class JSONBType(TypeDecorator):
    """Portable JSONB: native JSONB on PostgreSQL, JSON elsewhere."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[override]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class Base(DeclarativeBase):
    """Declarative base for all zubot-ingestion ORM models."""


class BatchORM(Base):
    """A submission batch — groups one or more PDF jobs."""

    __tablename__ = "batches"

    id: Mapped[UUID] = mapped_column(
        UUIDType(),
        primary_key=True,
    )
    deployment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    node_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    total_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    jobs: Mapped[list[JobORM]] = relationship(
        "JobORM",
        back_populates="batch",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class JobORM(Base):
    """A single PDF extraction job."""

    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(
        UUIDType(),
        primary_key=True,
    )
    batch_id: Mapped[UUID] = mapped_column(
        UUIDType(),
        ForeignKey("batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONBType(), nullable=True)
    confidence_tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(
        Numeric(4, 3), nullable=True
    )
    processing_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    otel_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    pipeline_trace: Mapped[dict[str, Any] | None] = mapped_column(
        JSONBType(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    batch: Mapped[BatchORM] = relationship("BatchORM", back_populates="jobs")
    review_actions: Mapped[list[ReviewActionORM]] = relationship(
        "ReviewActionORM",
        back_populates="job",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_jobs_batch_id", "batch_id"),
        Index("idx_jobs_status", "status"),
        Index("idx_jobs_file_hash", "file_hash"),
        Index("idx_jobs_confidence_tier", "confidence_tier"),
    )


class ReviewActionORM(Base):
    """Audit record for an approve/reject decision on a REVIEW-tier job."""

    __tablename__ = "review_actions"

    id: Mapped[UUID] = mapped_column(
        UUIDType(),
        primary_key=True,
    )
    job_id: Mapped[UUID] = mapped_column(
        UUIDType(),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    corrections: Mapped[dict[str, Any] | None] = mapped_column(
        JSONBType(), nullable=True
    )
    reviewed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    job: Mapped[JobORM] = relationship("JobORM", back_populates="review_actions")
