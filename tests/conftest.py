"""Shared PostgreSQL fixtures for automated integration tests.

By default tests start an embedded PostgreSQL 16 server via ``pgserver`` and
create a fresh database for every test.  Set ``TEST_POSTGRES_ADMIN_URL`` to an
existing PostgreSQL superuser URL to use a managed/local server instead.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Configuration validation requires these values even though no external API
# calls are made by the test suite.  DOTENV_OVERRIDE=false keeps a developer's
# local .env from replacing the test values.
os.environ.setdefault("DOTENV_OVERRIDE", "false")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres@localhost/postgres")
os.environ.setdefault("META_PAGE_ID", "test-page")
os.environ.setdefault("META_PAGE_ACCESS_TOKEN", "test-token")
os.environ.setdefault("META_FORM_ID", "test-form")
os.environ.setdefault("MS_TENANT_ID", "test-tenant")
os.environ.setdefault("MS_CLIENT_ID", "test-client")
os.environ.setdefault("MS_CLIENT_SECRET", "test-secret")
os.environ.setdefault("MS_SENDER_EMAIL", "test@example.com")
os.environ.setdefault("MS_RECIPIENT_EMAIL", "test@example.com")
os.environ.setdefault("BOOKING_URL", "https://example.com/book")
os.environ["DASHBOARD_SECRET_KEY"] = "test-dashboard-secret-with-enough-entropy"
os.environ["DASHBOARD_USERNAME"] = "admin"
os.environ["DASHBOARD_PASSWORD"] = "TestOnly-Strong-9f3a7c"
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ.setdefault("WEBHOOK_API_KEY", "test-webhook-key")
os.environ.setdefault("CRON_SECRET", "test-cron-secret")


def _normalise(url: str) -> str:
    return url.replace("postgres://", "postgresql+pg8000://").replace(
        "postgresql://", "postgresql+pg8000://"
    )


def _reset_app_state() -> None:
    from database import reset_database
    from db.session import dispose_engine

    reset_database()
    dispose_engine()
    sys.modules.pop("dashboard", None)


@pytest.fixture(scope="session")
def postgres_admin_url() -> Iterator[str]:
    external = os.getenv("TEST_POSTGRES_ADMIN_URL", "").strip()
    if external:
        yield _normalise(external)
        return

    import pgserver

    pgdata = Path(os.getenv("TEST_PGDATA", os.path.join(str(ROOT), ".test-pgdata")))
    pgdata.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(pgdata))
    try:
        yield _normalise(server.get_uri())
    finally:
        server.cleanup()


@pytest.fixture
def pg_database(postgres_admin_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Create an isolated, Alembic-migrated PostgreSQL database per test."""
    db_name = f"opencrm_test_{uuid.uuid4().hex[:12]}"
    admin_engine = create_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
    base_url = postgres_admin_url.rsplit("/", 1)[0]
    test_url = f"{base_url}/{db_name}"

    try:
        with admin_engine.connect() as admin:
            admin.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
            admin.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        admin_engine.dispose()

    monkeypatch.setenv("DATABASE_URL", test_url)
    _reset_app_state()

    old_cwd = os.getcwd()
    try:
        os.chdir(ROOT)
        alembic_config = Config(str(ROOT / "alembic.ini"))
        command.upgrade(alembic_config, "head")
        yield test_url
    finally:
        os.chdir(old_cwd)
        _reset_app_state()
        cleanup_engine = create_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
        try:
            with cleanup_engine.connect() as admin:
                admin.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        finally:
            cleanup_engine.dispose()


@pytest.fixture
def client(pg_database: str):
    """Authenticated Flask test client backed by an isolated database."""
    import dashboard

    dashboard._bootstrapped = False
    app = dashboard.app
    app.config.update(TESTING=True)
    test_client = app.test_client()
    response = test_client.post(
        "/login", data={"username": "admin", "password": os.environ["DASHBOARD_PASSWORD"]}
    )
    assert response.status_code in (200, 302)
    yield test_client
    dashboard.dispose_dashboard_engine()
