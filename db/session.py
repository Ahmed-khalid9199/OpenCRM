"""Engine and session management for the consolidated PostgreSQL data layer.

Serverless behaviour
--------------------
When ``DB_SERVERLESS_MODE=true`` (the setting to use on Vercel), SQLAlchemy
uses ``NullPool``.  Each function invocation gets a fresh physical connection
which is returned to PostgreSQL immediately, avoiding idle-connection buildup
across thousands of short-lived function instances.

For long-running self-hosted workers, a bounded ``QueuePool`` is used with the
pool sizes configured through ``DB_POOL_SIZE`` / ``DB_MAX_OVERFLOW``.
"""

import os
import re
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool


_lock = threading.Lock()
_engine: Optional[Engine] = None
_engine_url: Optional[str] = None

SessionFactory = sessionmaker(autoflush=False, expire_on_commit=False, future=True)
SessionLocal = scoped_session(SessionFactory)


def normalize_database_url(url: str) -> str:
    """Normalise common DATABASE_URL spellings to the psycopg 3 driver."""
    if not url or not url.strip():
        raise RuntimeError(
            "DATABASE_URL is not configured. Set it to a PostgreSQL URL, e.g. "
            "postgresql+psycopg://user:password@host:5432/database"
        )
    url = url.strip()
    generic_scheme = next(
        (scheme for scheme in ("postgres://", "postgresql://") if url.startswith(scheme)),
        None,
    )
    if generic_scheme is None:
        return url

    driver = os.getenv("DB_DRIVER", "postgresql+psycopg").strip() or "postgresql+psycopg"
    if not driver.startswith("postgresql+"):
        raise RuntimeError(
            "DB_DRIVER must be a SQLAlchemy PostgreSQL driver such as "
            "postgresql+psycopg or postgresql+psycopg2"
        )
    return f"{driver}://{url[len(generic_scheme):]}"


def get_database_url() -> str:
    """Return the normalised DATABASE_URL from the environment."""
    return normalize_database_url(os.getenv("DATABASE_URL", ""))


def mask_database_url(url: str) -> str:
    """Return a URL safe to print in logs and CLI output."""
    if not url:
        return "<not configured>"
    match = re.search(r"://([^@/]+)@", url)
    if not match:
        return url
    return url[: match.start(1)] + "***:***" + url[match.end(1) :]


_URL_WITH_CREDENTIALS = re.compile(
    r"([A-Za-z][A-Za-z0-9+.-]*://)([^:@/\s]+):([^@/\s]+)@"
)


def redact_credentials(text: object) -> str:
    """Remove credentials from exception text before it is printed or logged."""
    result = "" if text is None else str(text)
    for env_name in ("DATABASE_URL", "TEST_POSTGRES_ADMIN_URL"):
        raw = os.getenv(env_name, "").strip()
        if raw:
            result = result.replace(raw, mask_database_url(raw))
    return _URL_WITH_CREDENTIALS.sub(r"\1***:***@", result)


def _get_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _make_engine(url: str) -> Engine:
    options = {
        "pool_pre_ping": True,
        "echo": _get_bool("DB_ECHO", False),
        "future": True,
    }

    if _get_bool("DB_SERVERLESS_MODE", False):
        options["poolclass"] = NullPool
    else:
        options.update(
            {
                "pool_size": max(1, _get_int("DB_POOL_SIZE", 5)),
                "max_overflow": max(0, _get_int("DB_MAX_OVERFLOW", 5)),
                "pool_timeout": max(1, _get_int("DB_POOL_TIMEOUT", 30)),
                "pool_recycle": max(30, _get_int("DB_POOL_RECYCLE", 1800)),
            }
        )

    sslmode = os.getenv("DB_SSLMODE", "").strip()
    connect_args = {}
    if sslmode:
        connect_args["sslmode"] = sslmode
    if connect_args:
        options["connect_args"] = connect_args

    application_name = os.getenv("DB_APPLICATION_NAME", "opencrm").strip()
    if application_name:
        options["connect_args"] = {**options.get("connect_args", {}), "application_name": application_name}

    return create_engine(url, **options)


def get_engine() -> Engine:
    """Return a process-wide engine for the current DATABASE_URL."""
    global _engine, _engine_url

    url = get_database_url()
    with _lock:
        if _engine is not None and _engine_url != url:
            SessionLocal.remove()
            _engine.dispose()
            _engine = None
        if _engine is None:
            _engine = _make_engine(url)
            _engine_url = url
            SessionFactory.configure(bind=_engine)
        return _engine


def get_session():
    """Return the scoped SQLAlchemy session for this thread/Flask request."""
    get_engine()
    return SessionLocal()


@contextmanager
def session_scope():
    """Transactional scope that commits on success and rolls back on error."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        remove_session()


def remove_session() -> None:
    """Release the current scoped session (call from Flask teardown)."""
    SessionLocal.remove()


def dispose_engine() -> None:
    """Dispose the cached engine. Intended for tests and process shutdown."""
    global _engine, _engine_url
    with _lock:
        SessionLocal.remove()
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _engine_url = None
