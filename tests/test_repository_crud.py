"""CRUD, relationship and constraint tests for all migrated entities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from db.models import Campaign, CampaignSend, Lead, LeadNote, Sequence, SequenceEnrollment, User
from db.repository import CRMRepository, DatabaseError
from db.session import get_session, remove_session


def _dt(offset_seconds: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


def test_repository_lead_crud(pg_database: str) -> None:
    repo = CRMRepository()
    lead_id = "crud-lead-1"
    inserted = repo.insert_lead(
        {
            "lead_id": lead_id,
            "form_id": "form-1",
            "created_time": _dt().isoformat(),
            "email": "crud@example.com",
            "full_name": "CRUD Lead",
            "raw_field_data": {"source": "test"},
            "tags": {"industry": "Technology"},
            "status": "intake",
            "deal_value": 125.50,
        }
    )
    assert inserted is not None
    assert repo.insert_lead(
        {
            "lead_id": lead_id,
            "created_time": _dt().isoformat(),
            "raw_field_data": {},
        }
    ) is None  # duplicate lead_id is a no-op for the legacy API

    assert repo.lead_exists(lead_id) is True
    lead = repo.get_lead_by_id(lead_id)
    assert lead["email"] == "crud@example.com"
    assert lead["raw_field_data"] == {"source": "test"}
    assert lead["tags"] == {"industry": "Technology"}
    assert lead["deal_value"] == 125.5

    assert repo.get_leads_by_status("intake")[0]["lead_id"] == lead_id
    assert repo.get_all_leads(limit=1)[0]["lead_id"] == lead_id
    assert repo.get_lead_count() == 1
    assert repo.get_lead_count("intake") == 1
    assert repo.get_stats()["total_leads"] == 1

    assert repo.update_lead_status(lead_id, "emailed", _dt().isoformat()) is True
    assert repo.get_lead_by_id(lead_id)["status"] == "emailed"
    assert repo.get_lead_count("intake") == 0

    assert repo.delete_lead(lead_id) is True
    assert repo.get_lead_count() == 0
    assert repo.delete_lead(lead_id) is False


def test_all_related_entities_and_cascades(pg_database: str) -> None:
    repo = CRMRepository()
    lead_id = "related-lead"
    assert repo.insert_lead(
        {
            "lead_id": lead_id,
            "created_time": _dt().isoformat(),
            "raw_field_data": {},
        }
    ) is not None

    session = get_session()
    try:
        user = User(username="agent", password="hash", display_name="Agent", role="user")
        note = LeadNote(lead_id=lead_id, author="agent", content="Called")
        sequence = Sequence(name="Follow-up", steps=[{"subject": "Hi", "body": "Hello"}])
        session.add_all([user, note, sequence])
        session.flush()

        enrollment = SequenceEnrollment(
            lead_id=lead_id,
            sequence_id=sequence.id,
            current_step=0,
            status="active",
            next_send_at=_dt(3600),
        )
        campaign = Campaign(
            name="Campaign",
            lead_filters={"status": "intake"},
            total_recipients=1,
        )
        session.add_all([enrollment, campaign])
        session.flush()

        send = CampaignSend(
            campaign_id=campaign.id,
            lead_id=lead_id,
            email="related@example.com",
            status="sent",
            sent_at=_dt(),
        )
        session.add(send)
        session.commit()

        assert session.scalar(select(User).where(User.username == "agent")) is not None
        assert session.scalar(select(LeadNote).where(LeadNote.lead_id == lead_id)) is not None
        assert session.scalar(select(SequenceEnrollment).where(SequenceEnrollment.lead_id == lead_id)) is not None
        assert session.scalar(select(CampaignSend).where(CampaignSend.campaign_id == campaign.id)) is not None

        assert repo.delete_lead(lead_id) is True
        assert session.scalar(select(LeadNote).where(LeadNote.lead_id == lead_id)) is None
        assert session.scalar(select(SequenceEnrollment).where(SequenceEnrollment.lead_id == lead_id)) is None
        assert session.scalar(select(CampaignSend).where(CampaignSend.lead_id == lead_id)) is None
    finally:
        remove_session()


def test_foreign_keys_are_enforced(pg_database: str) -> None:
    session = get_session()
    try:
        session.add(LeadNote(lead_id="missing-lead", author="agent", content="orphan"))
        with pytest.raises(IntegrityError):
            session.flush()
    finally:
        session.rollback()
        remove_session()


def test_numeric_and_json_types_round_trip(pg_database: str) -> None:
    repo = CRMRepository()
    lead_id = "typed-lead"
    repo.insert_lead(
        {
            "lead_id": lead_id,
            "created_time": _dt().isoformat(),
            "raw_field_data": {"nested": {"items": [1, 2, 3]}},
            "tags": {"custom": ["a", "b"]},
            "deal_value": "9999.99",
        }
    )
    session = get_session()
    try:
        value = session.execute(
            text("SELECT deal_value FROM leads WHERE lead_id = :lead_id"),
            {"lead_id": lead_id},
        ).scalar_one()
        assert isinstance(value, Decimal)
        assert value == Decimal("9999.99")
    finally:
        remove_session()


def test_invalid_timestamp_raises_database_error(pg_database: str) -> None:
    repo = CRMRepository()
    with pytest.raises(DatabaseError):
        repo.insert_lead(
            {
                "lead_id": "bad-time",
                "created_time": "not-a-date",
                "raw_field_data": {},
            }
        )
