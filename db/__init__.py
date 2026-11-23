"""Consolidated SQLAlchemy data-access layer for Arkitekt OpenCRM."""

from .models import Base
from .session import (
    SessionLocal,
    dispose_engine,
    get_database_url,
    get_engine,
    get_session,
    mask_database_url,
    remove_session,
    session_scope,
)
from .repository import CRMRepository, DatabaseError

__all__ = [
    "Base",
    "SessionLocal",
    "CRMRepository",
    "DatabaseError",
    "dispose_engine",
    "get_database_url",
    "get_engine",
    "get_session",
    "mask_database_url",
    "remove_session",
    "session_scope",
]
