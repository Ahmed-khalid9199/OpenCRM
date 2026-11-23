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
import ssl
import threading
from contextlib import contextmanager
from typing import Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool


_lock = threading.Lock()
_engine: Optional[Engine] = None
_engine_url: Optional[str] = None

SessionFactory = sessionmaker(autoflush=False, expire_on_commit=False, future=True)
SessionLocal = scoped_session(SessionFactory)


def normalize_database_url(url: str) -> str:
    """Normalise common DATABASE_URL spellings to the configured SQLAlchemy driver."""
    if not url or not url.strip():
        raise RuntimeError(
            "DATABASE_URL is not configured. Set it to a PostgreSQL URL, e.g. "
            "postgresql+pg8000://user:password@host:5432/database"
        )
    url = url.strip()
    generic_scheme = next(
        (scheme for scheme in ("postgres://", "postgresql://") if url.startswith(scheme)),
        None,
    )
    if generic_scheme is None:
        return url

    driver = os.getenv("DB_DRIVER", "postgresql+pg8000").strip() or "postgresql+pg8000"
    if not driver.startswith("postgresql+"):
        raise RuntimeError(
            "DB_DRIVER must be a SQLAlchemy PostgreSQL driver such as "
            "postgresql+pg8000 or postgresql+psycopg"
        )
    normalised = f"{driver}://{url[len(generic_scheme):]}"
    return _drop_libpq_only_options(normalised, driver)


def _drop_libpq_only_options(url: str, driver: str) -> str:
    """Remove libpq-only URL query options that pg8000 cannot accept."""
    if driver != "postgresql+pg8000":
        return url
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    libpq_only = {"sslmode", "channel_binding"}
    kept = [(key, value) for key, value in pairs if key.lower() not in libpq_only]
    if len(kept) == len(pairs):
        return url
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )


def get_database_url() -> str:
    """Return the normalised DATABASE_URL from the environment."""
    url = normalize_database_url(os.getenv("DATABASE_URL", ""))
    return _drop_libpq_only_options(url, "postgresql+pg8000")


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


def _register_pg8000_error_mapping(engine: Engine) -> None:
    """Map pg8000 SQLSTATE 23xxx failures to SQLAlchemy IntegrityError.

    pg8000 reports constraint violations as ``ProgrammingError`` even though
    the SQLSTATE identifies them as integrity errors.  The application's
    repository code relies on SQLAlchemy's ``IntegrityError`` for duplicate
    leads and foreign-key failures, so translate at the engine boundary.
    """

    @event.listens_for(engine, "handle_error")
    def _map_integrity_errors(context) -> None:
        original = context.original_exception
        args = getattr(original, "args", None)
        details = args[0] if args else None
        sqlstate = details.get("C") if isinstance(details, dict) else None
        if isinstance(sqlstate, str) and sqlstate.startswith("23"):
            raise IntegrityError(context.statement, context.parameters, original) from original


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

    sslmode = os.getenv("DB_SSLMODE", "").strip().lower()
    connect_args = {}
    if "pg8000" in url:
        # pg8000 does not understand libpq's sslmode query parameter. Build an
        # SSL context instead; DB_SSLMODE is still the single config switch.
        if sslmode and sslmode != "disable":
            connect_args["ssl_context"] = ssl.create_default_context()
    elif sslmode:
        connect_args["sslmode"] = sslmode

    application_name = os.getenv("DB_APPLICATION_NAME", "opencrm").strip()
    if application_name:
        connect_args["application_name"] = application_name

    if connect_args:
        options["connect_args"] = connect_args

    engine = create_engine(url, **options)
    if "pg8000" in url:
        _register_pg8000_error_mapping(engine)
    return engine


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
