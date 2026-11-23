"""Concurrent access tests for the pooled SQLAlchemy data layer."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from db.repository import CRMRepository
from db.session import get_engine


def _insert(index: int) -> str:
    lead_id = f"concurrent-{index}"
    repo = CRMRepository()
    result = repo.insert_lead(
        {
            "lead_id": lead_id,
            "created_time": datetime.now(timezone.utc).isoformat(),
            "raw_field_data": {"index": index},
            "status": "intake",
        }
    )
    return lead_id if result else ""


def test_concurrent_inserts_are_complete(pg_database: str) -> None:
    total = 40
    with ThreadPoolExecutor(max_workers=8) as executor:
        inserted_ids = list(executor.map(_insert, range(total)))

    assert all(inserted_ids)
    assert len(set(inserted_ids)) == total

    repo = CRMRepository()
    assert repo.get_lead_count() == total
    assert len(repo.get_all_leads(limit=100)) == total

    with get_engine().connect() as connection:
        pool = connection.engine.pool
        assert pool is not None
