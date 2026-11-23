"""Alembic migration tests against a real PostgreSQL database."""

from __future__ import annotations

import os

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from db.models import Base
from db.session import get_database_url

EXPECTED_TABLES = {
    "alembic_version",
    "app_settings",
    "campaign_sends",
    "campaigns",
    "lead_notes",
    "leads",
    "sequence_enrollments",
    "sequences",
    "users",
}


def _types(table: str) -> dict:
    """Return reflected SQLAlchemy column types for a migrated table."""
    engine = create_engine(get_database_url())
    try:
        with engine.connect() as connection:
            return {col["name"]: col["type"] for col in inspect(connection).get_columns(table)}
    finally:
        engine.dispose()


def test_alembic_creates_complete_schema(pg_database: str) -> None:
    engine = create_engine(pg_database)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert set(inspector.get_table_names()) == EXPECTED_TABLES

            for table_name, table in Base.metadata.tables.items():
                actual_nullable = {
                    column["name"]: column["nullable"]
                    for column in inspector.get_columns(table_name)
                }
                expected_nullable = {column.name: column.nullable for column in table.columns}
                assert actual_nullable == expected_nullable, table_name

            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0002_app_settings"

            leads = {col["name"] for col in inspector.get_columns("leads")}
            assert leads == {
                "id", "lead_id", "form_id", "created_time", "email", "full_name",
                "first_name", "last_name", "phone_number", "company_name",
                "job_title", "raw_field_data", "status", "email_sent_at",
                "inserted_at", "updated_at", "tags", "lead_source",
                "follow_up_date", "deal_value", "expected_close_date", "assigned_to",
            }

            lead_indexes = {idx["name"] for idx in inspector.get_indexes("leads")}
            assert {"idx_lead_id", "idx_status", "idx_email", "idx_created_time", "idx_status_created"} <= lead_indexes

            fk_targets = {
                (fk["constrained_columns"][0], fk["referred_table"])
                for table in ("lead_notes", "sequence_enrollments", "campaign_sends")
                for fk in inspector.get_foreign_keys(table)
            }
            assert ("lead_id", "leads") in fk_targets
            assert ("sequence_id", "sequences") in fk_targets
            assert ("campaign_id", "campaigns") in fk_targets
    finally:
        engine.dispose()


def test_postgres_native_types_are_used(pg_database: str) -> None:
    leads = _types("leads")
    assert "JSONB" in str(leads["raw_field_data"]).upper()
    assert "JSONB" in str(leads["tags"]).upper()
    assert "TIMESTAMP" in str(leads["created_time"]).upper()
    # SQLAlchemy renders TIMESTAMPTZ's type name as TIMESTAMP; the timezone
    # flag is what distinguishes it from TIMESTAMP WITHOUT TIME ZONE.
    assert getattr(leads["created_time"], "timezone", False) is True
    assert "NUMERIC" in str(leads["deal_value"]).upper()

    sequences = _types("sequences")
    assert "JSONB" in str(sequences["steps"]).upper()

    campaigns = _types("campaigns")
    assert "JSONB" in str(campaigns["lead_filters"]).upper()


def test_downgrade_and_upgrade_are_reversible(pg_database: str) -> None:
    config = Config(os.path.join(str(os.getcwd()), "alembic.ini"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    engine = create_engine(pg_database)
    try:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert EXPECTED_TABLES <= tables
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0002_app_settings"
    finally:
        engine.dispose()
