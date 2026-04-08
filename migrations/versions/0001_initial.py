"""initial schema: batches, jobs, review_actions

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-07 00:00:00.000000

Implements CAP-004: PostgreSQL persistence schema.

This migration:
1. Enables uuid-ossp extension for uuid_generate_v4()
2. Creates batches, jobs, review_actions tables
3. Adds the four required jobs indexes
4. Defines update_updated_at() trigger function
5. Attaches BEFORE UPDATE triggers to batches and jobs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- extensions ------------------------------------------------------
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    # ---- batches ---------------------------------------------------------
    op.create_table(
        "batches",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("deployment_id", sa.String(length=255), nullable=True),
        sa.Column("node_id", sa.String(length=255), nullable=True),
        sa.Column("callback_url", sa.Text(), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("total_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # ---- jobs ------------------------------------------------------------
    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column(
            "batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("confidence_tier", sa.String(length=16), nullable=True),
        sa.Column("confidence_score", sa.Numeric(4, 3), nullable=True),
        sa.Column("processing_time_ms", sa.Integer(), nullable=True),
        sa.Column("otel_trace_id", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("pipeline_trace", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_jobs_batch_id", "jobs", ["batch_id"])
    op.create_index("idx_jobs_status", "jobs", ["status"])
    op.create_index("idx_jobs_file_hash", "jobs", ["file_hash"])
    op.create_index("idx_jobs_confidence_tier", "jobs", ["confidence_tier"])

    # ---- review_actions --------------------------------------------------
    op.create_table(
        "review_actions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("corrections", postgresql.JSONB(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # ---- updated_at trigger ---------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION update_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_batches_updated_at
        BEFORE UPDATE ON batches
        FOR EACH ROW
        EXECUTE FUNCTION update_updated_at();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_jobs_updated_at
        BEFORE UPDATE ON jobs
        FOR EACH ROW
        EXECUTE FUNCTION update_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_jobs_updated_at ON jobs")
    op.execute("DROP TRIGGER IF EXISTS trg_batches_updated_at ON batches")
    op.execute("DROP FUNCTION IF EXISTS update_updated_at()")

    op.drop_table("review_actions")

    op.drop_index("idx_jobs_confidence_tier", table_name="jobs")
    op.drop_index("idx_jobs_file_hash", table_name="jobs")
    op.drop_index("idx_jobs_status", table_name="jobs")
    op.drop_index("idx_jobs_batch_id", table_name="jobs")
    op.drop_table("jobs")

    op.drop_table("batches")
    # uuid-ossp extension intentionally left in place
