"""SQLAlchemy models for the complete OpenCRM schema (PostgreSQL).

Every field from the original SQLite schema is preserved.  SQLite-specific
storage types are mapped to PostgreSQL equivalents:

* JSON-in-TEXT  -> JSONB
* TEXT timestamps -> TIMESTAMPTZ
* REAL money    -> NUMERIC(14, 2)

No tenant column is introduced.  A future multi-tenant migration can add one
canonical ``tenant_id`` foreign key and a single Alembic revision.
"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Single declarative base shared by the application and Alembic."""


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        Index("idx_lead_id", "lead_id"),
        Index("idx_status", "status"),
        Index("idx_email", "email"),
        Index("idx_created_time", "created_time"),
        Index("idx_status_created", "status", "created_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    form_id: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    created_time: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    email: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    full_name: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    first_name: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    last_name: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    phone_number: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    company_name: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    job_title: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    raw_field_data: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="new", server_default=text("'new'")
    )
    email_sent_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))
    inserted_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    tags: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    lead_source: Mapped[str] = mapped_column(
        String, nullable=False, default="meta", server_default=text("'meta'")
    )
    follow_up_date: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))
    deal_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    expected_close_date: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))
    assigned_to: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=text("''")
    )

    notes: Mapped[list["LeadNote"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", passive_deletes=True
    )
    enrollments: Mapped[list["SequenceEnrollment"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", passive_deletes=True
    )
    campaign_sends: Mapped[list["CampaignSend"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", passive_deletes=True
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=text("''")
    )
    role: Mapped[str] = mapped_column(
        String, nullable=False, default="user", server_default=text("'user'")
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )


class LeadNote(Base):
    __tablename__ = "lead_notes"
    __table_args__ = (Index("idx_lead_notes_lead_id", "lead_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("leads.lead_id", ondelete="CASCADE"),
        nullable=False,
    )
    author: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=text("''")
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    lead: Mapped[Lead] = relationship(back_populates="notes")


class Sequence(Base):
    __tablename__ = "sequences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    steps: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    enrollments: Mapped[list["SequenceEnrollment"]] = relationship(
        back_populates="sequence", cascade="all, delete-orphan", passive_deletes=True
    )


class SequenceEnrollment(Base):
    __tablename__ = "sequence_enrollments"
    __table_args__ = (
        Index("idx_enrollments_lead", "lead_id"),
        Index("idx_enrollments_next_send", "status", "next_send_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("leads.lead_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sequences.id", ondelete="CASCADE"),
        nullable=False,
    )
    current_step: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default=text("'active'")
    )
    next_send_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))
    enrolled_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    lead: Mapped[Lead] = relationship(back_populates="enrollments")
    sequence: Mapped[Sequence] = relationship(back_populates="enrollments")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    campaign_type: Mapped[str] = mapped_column(
        String, nullable=False, default="single", server_default=text("'single'")
    )
    email_provider: Mapped[str] = mapped_column(
        String, nullable=False, default="outlook", server_default=text("'outlook'")
    )
    subject_template: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    body_template: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    lead_filters: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="draft", server_default=text("'draft'")
    )
    total_recipients: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    sent_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    failed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
    sent_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))

    sends: Mapped[list["CampaignSend"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )


class CampaignSend(Base):
    __tablename__ = "campaign_sends"
    __table_args__ = (
        Index("idx_campaign_sends_campaign", "campaign_id"),
        Index("idx_campaign_sends_status", "campaign_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("leads.lead_id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default=text("'pending'")
    )
    sent_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True))
    error: Mapped[Optional[str]] = mapped_column(Text)

    campaign: Mapped[Campaign] = relationship(back_populates="sends")
    lead: Mapped[Lead] = relationship(back_populates="campaign_sends")
