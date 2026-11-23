"""Initial PostgreSQL schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-23

Baselines every entity from the original SQLite database with PostgreSQL-native
types.  Field names are intentionally unchanged so application logic and the
SQLite migration utility can map rows one-to-one.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lead_id", sa.String(), nullable=False),
        sa.Column("form_id", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("email", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("full_name", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("first_name", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("last_name", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("phone_number", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("company_name", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("job_title", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "raw_field_data",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(), server_default=sa.text("'new'"), nullable=False),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "inserted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("lead_source", sa.String(), server_default=sa.text("'meta'"), nullable=False),
        sa.Column("follow_up_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deal_value", sa.Numeric(14, 2), nullable=True),
        sa.Column("expected_close_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_to", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lead_id"),
    )
    op.create_index("idx_lead_id", "leads", ["lead_id"], unique=False)
    op.create_index("idx_status", "leads", ["status"], unique=False)
    op.create_index("idx_email", "leads", ["email"], unique=False)
    op.create_index("idx_created_time", "leads", ["created_time"], unique=False)
    op.create_index("idx_status_created", "leads", ["status", "created_time"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("role", sa.String(), server_default=sa.text("'user'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "lead_notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "lead_id",
            sa.String(),
            sa.ForeignKey("leads.lead_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("author", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_lead_notes_lead_id", "lead_notes", ["lead_id"], unique=False)

    op.create_table(
        "sequences",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "steps",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "sequence_enrollments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "lead_id",
            sa.String(),
            sa.ForeignKey("leads.lead_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sequence_id",
            sa.Integer(),
            sa.ForeignKey("sequences.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("current_step", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "enrolled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_enrollments_lead", "sequence_enrollments", ["lead_id"], unique=False)
    op.create_index(
        "idx_enrollments_next_send",
        "sequence_enrollments",
        ["status", "next_send_at"],
        unique=False,
    )

    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("campaign_type", sa.String(), server_default=sa.text("'single'"), nullable=False),
        sa.Column("email_provider", sa.String(), server_default=sa.text("'outlook'"), nullable=False),
        sa.Column("subject_template", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("body_template", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "lead_filters",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("total_recipients", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sent_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "campaign_sends",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "lead_id",
            sa.String(),
            sa.ForeignKey("leads.lead_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_campaign_sends_campaign", "campaign_sends", ["campaign_id"], unique=False)
    op.create_index(
        "idx_campaign_sends_status",
        "campaign_sends",
        ["campaign_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_campaign_sends_status", table_name="campaign_sends")
    op.drop_index("idx_campaign_sends_campaign", table_name="campaign_sends")
    op.drop_table("campaign_sends")

    op.drop_table("campaigns")

    op.drop_index("idx_enrollments_next_send", table_name="sequence_enrollments")
    op.drop_index("idx_enrollments_lead", table_name="sequence_enrollments")
    op.drop_table("sequence_enrollments")

    op.drop_table("sequences")

    op.drop_index("idx_lead_notes_lead_id", table_name="lead_notes")
    op.drop_table("lead_notes")

    op.drop_table("users")

    op.drop_index("idx_status_created", table_name="leads")
    op.drop_index("idx_created_time", table_name="leads")
    op.drop_index("idx_email", table_name="leads")
    op.drop_index("idx_status", table_name="leads")
    op.drop_index("idx_lead_id", table_name="leads")
    op.drop_table("leads")
