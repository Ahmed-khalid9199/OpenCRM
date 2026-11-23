"""Compatibility facade for the consolidated PostgreSQL data-access layer.

Historically this module owned a SQLite connection, schema creation and all
lead CRUD operations.  It is now a thin compatibility layer over
:class:`db.repository.CRMRepository`, which uses the single SQLAlchemy engine
and Alembic-managed schema in :mod:`db`.

The original implementation is retained unchanged in
``legacy_sqlite_database.py`` until the PostgreSQL migration has been verified
end to end.  That module is not imported by the running application.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from db.repository import CRMRepository, DatabaseError
from db.session import dispose_engine, get_database_url, mask_database_url

logger = logging.getLogger(__name__)


class Database:
    """Backward-compatible facade exposing the legacy ``Database`` API."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            logger.warning(
                "db_path=%s is ignored; the application now uses DATABASE_URL",
                db_path,
            )
        self._repository = CRMRepository()
        self.db_path = self._repository.db_path
        logger.info(
            "Database initialized successfully: %s",
            mask_database_url(get_database_url()),
        )

    def insert_lead(self, lead_data: Dict[str, Any]) -> Optional[int]:
        return self._repository.insert_lead(lead_data)

    def lead_exists(self, lead_id: str) -> bool:
        return self._repository.lead_exists(lead_id)

    def update_lead_status(
        self,
        lead_id: str,
        status: str,
        email_sent_at: Optional[str] = None,
    ) -> bool:
        return self._repository.update_lead_status(lead_id, status, email_sent_at)

    def get_leads_by_status(
        self, status: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        return self._repository.get_leads_by_status(status, limit)

    def get_lead_by_id(self, lead_id: str) -> Optional[Dict[str, Any]]:
        return self._repository.get_lead_by_id(lead_id)

    def get_all_leads(
        self, limit: Optional[int] = None, offset: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        return self._repository.get_all_leads(limit, offset)

    def get_lead_count(self, status: Optional[str] = None) -> int:
        return self._repository.get_lead_count(status)

    def delete_lead(self, lead_id: str) -> bool:
        return self._repository.delete_lead(lead_id)

    def get_stats(self) -> Dict[str, Any]:
        return self._repository.get_stats()

    def vacuum(self) -> bool:
        return self._repository.vacuum()

    def close(self) -> None:
        self._repository.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


_db_instance: Optional[Database] = None
_db_lock = threading.Lock()


def get_database() -> Database:
    """Return a process-wide compatibility ``Database`` instance."""
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                _db_instance = Database()
    return _db_instance


def reset_database() -> None:
    """Dispose cached engine/state. Intended for tests and process shutdown."""
    global _db_instance
    with _db_lock:
        _db_instance = None
        dispose_engine()


__all__ = [
    "Database",
    "DatabaseError",
    "get_database",
    "reset_database",
]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = Database()
    print("Database URL:", mask_database_url(get_database_url()))
    print("Statistics:", db.get_stats())
    sample_lead = {
        "lead_id": f"test_lead_{int(datetime.utcnow().timestamp())}",
        "form_id": "test_form",
        "created_time": datetime.utcnow().isoformat(),
        "email": "test@example.com",
        "full_name": "Test User",
        "raw_field_data": {"test": "data"},
        "status": "new",
    }
    print("Insert ID:", db.insert_lead(sample_lead))
    print("Exists:", db.lead_exists(sample_lead["lead_id"]))
    print("Retrieved:", db.get_lead_by_id(sample_lead["lead_id"]))
