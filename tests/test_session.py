"""Engine configuration tests for pooled and serverless modes."""

from __future__ import annotations

from sqlalchemy.pool import NullPool, QueuePool

from db.session import (
    dispose_engine,
    get_engine,
    mask_database_url,
    normalize_database_url,
    redact_credentials,
)


def test_serverless_mode_uses_null_pool(pg_database: str, monkeypatch) -> None:
    dispose_engine()
    monkeypatch.setenv("DB_SERVERLESS_MODE", "true")
    try:
        assert isinstance(get_engine().pool, NullPool)
    finally:
        dispose_engine()


def test_generic_postgres_url_uses_configured_driver(monkeypatch) -> None:
    monkeypatch.setenv("DB_DRIVER", "postgresql+pg8000")
    assert (
        normalize_database_url("postgres://user:password@localhost:5432/opencrm")
        == "postgresql+pg8000://user:password@localhost:5432/opencrm"
    )


def test_pg8000_url_drops_libpq_only_query_options(monkeypatch) -> None:
    monkeypatch.setenv("DB_DRIVER", "postgresql+pg8000")
    assert (
        normalize_database_url(
            "postgresql://user:password@localhost:5432/opencrm"
            "?sslmode=require&channel_binding=require&application_name=opencrm"
        )
        == "postgresql+pg8000://user:password@localhost:5432/opencrm?application_name=opencrm"
    )


def test_redact_credentials_hides_urls_in_exception_text(monkeypatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://alice:s3cret@db.example.com:5432/opencrm"
    )
    message = (
        "could not connect to postgresql+psycopg://alice:s3cret@db.example.com:5432/opencrm "
        "or postgres://bob:hunter2@other.example.com/reporting"
    )
    redacted = redact_credentials(message)
    assert "s3cret" not in redacted
    assert "hunter2" not in redacted
    assert "***:***" in redacted


def test_database_url_masking_hides_credentials() -> None:
    masked = mask_database_url("postgresql+psycopg://alice:s3cret@db.example.com:5432/opencrm")
    assert "s3cret" not in masked
    assert "alice" not in masked
    assert "***:***" in masked


def test_default_mode_uses_queue_pool(pg_database: str, monkeypatch) -> None:
    dispose_engine()
    monkeypatch.setenv("DB_SERVERLESS_MODE", "false")
    try:
        assert isinstance(get_engine().pool, QueuePool)
    finally:
        dispose_engine()
