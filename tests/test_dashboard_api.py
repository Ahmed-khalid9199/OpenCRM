"""Dashboard API tests: auth, filters, reporting, sequences and campaigns."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest


def _login(client, username: str = "admin", password: str = "TestOnly-Strong-9f3a7c"):
    return client.post("/login", data={"username": username, "password": password})


def test_authentication_and_authorization(client) -> None:
    import dashboard

    anonymous = dashboard.app.test_client()
    assert anonymous.get("/api/leads").status_code == 401

    assert _login(client).status_code in (200, 302)

    session = dashboard.get_session() if hasattr(dashboard, "get_session") else None
    # Insert a non-admin directly using the shared models.
    from db.models import User
    from db.session import get_session, remove_session

    db_session = session or get_session()
    try:
        db_session.add(
            User(
                username="viewer",
                password=dashboard._hash_password("viewer-pass"),
                display_name="Viewer",
                role="user",
            )
        )
        db_session.commit()
    finally:
        remove_session()

    viewer = dashboard.app.test_client()
    assert _login(viewer, "viewer", "viewer-pass").status_code in (200, 302)
    assert viewer.get("/api/users").status_code == 403
    assert client.get("/api/users").status_code == 200


def test_lead_filtering_search_and_reports(client) -> None:
    response = client.post(
        "/api/lead",
        json={
            "email": "filter@example.com",
            "first_name": "Filter",
            "last_name": "Tester",
            "company_name": "Acme",
            "status": "intake",
            "deal_value": 250.0,
            "lead_source": "manual",
        },
    )
    assert response.status_code in (200, 201)
    lead_id = response.get_json()["lead_id"]

    assert client.get("/api/leads", query_string={"status": "intake"}).status_code == 200
    filtered = client.get("/api/leads", query_string={"search": "Acme"}).get_json()
    assert filtered["total"] == 1
    assert filtered["leads"][0]["lead_id"] == lead_id

    assert client.get("/api/leads", query_string={"search": "no-match"}).get_json()["total"] == 0
    assert client.get("/api/lead/" + lead_id).status_code == 200
    assert client.get("/api/kanban").status_code == 200
    assert client.get("/api/leads/export/csv").status_code == 200

    stats = client.get("/api/stats").get_json()
    assert stats["total"] == 1
    for path in (
        "/api/reports/funnel",
        "/api/reports/by-source",
        "/api/reports/over-time",
        "/api/reports/pipeline-value",
        "/api/reminders",
    ):
        assert client.get(path).status_code == 200, path


def test_notes_follow_up_and_deal_updates(client) -> None:
    lead_id = client.post(
        "/api/lead", json={"email": "notes@example.com", "first_name": "Notes"}
    ).get_json()["lead_id"]

    assert client.patch(f"/api/lead/{lead_id}/status", json={"status": "contacted"}).status_code == 200
    assert client.post(f"/api/lead/{lead_id}/notes", json={"content": "Called", "author": "admin"}).status_code in (200, 201)
    notes = client.get(f"/api/lead/{lead_id}/notes").get_json()["notes"]
    assert len(notes) == 1 and notes[0]["content"] == "Called"
    assert client.delete(f"/api/lead/{lead_id}/notes/{notes[0]['id']}").status_code == 200

    now = datetime.now(timezone.utc).isoformat()
    assert client.patch(f"/api/lead/{lead_id}/follow-up", json={"follow_up_date": now}).status_code == 200
    assert client.patch(
        f"/api/lead/{lead_id}/deal",
        json={"deal_value": 42.5, "expected_close_date": now},
    ).status_code == 200
    assert client.patch(
        f"/api/lead/{lead_id}/tags", json={"tags": {"industry": "Technology"}}
    ).status_code == 200


def test_sequences_and_campaign_queries(client) -> None:
    lead_id = client.post(
        "/api/lead", json={"email": "sequence@example.com", "first_name": "Sequence"}
    ).get_json()["lead_id"]

    sequence = client.post(
        "/api/sequences",
        json={
            "name": "Welcome",
            "steps": [{"delay_days": 0, "subject": "Hello", "body": "Welcome!"}],
        },
    )
    assert sequence.status_code in (200, 201)
    seq_id = sequence.get_json()["id"]

    assert client.post(
        f"/api/lead/{lead_id}/enroll", json={"sequence_id": str(seq_id)}
    ).status_code in (200, 201)
    assert client.get(f"/api/lead/{lead_id}/enrollments").status_code == 200
    assert client.get("/api/sequences").get_json()["sequences"][0]["active_enrollments"] == 1
    assert client.post(f"/api/lead/{lead_id}/unenroll", json={"sequence_id": seq_id}).status_code == 200

    campaign = client.post(
        "/api/campaigns",
        json={"name": "Newsletter", "subject_template": "Hi", "body_template": "Body"},
    )
    assert campaign.status_code in (200, 201)
    cid = campaign.get_json()["id"]
    assert client.get(f"/api/campaigns/{cid}/preview").status_code == 200
    assert client.get(f"/api/campaigns/{cid}/stats").status_code == 200
    assert client.post(
        "/api/campaigns/filter-preview", json={"filters": {"status": "intake"}}
    ).status_code == 200
    assert client.get("/api/campaigns").status_code == 200


def test_kanban_labels_are_persisted(client) -> None:
    assert client.get("/api/kanban").get_json()["labels"] == {}

    response = client.post(
        "/api/kanban/labels", json={"status": "intake", "label": "New Leads"}
    )
    assert response.status_code == 200
    assert response.get_json()["labels"]["intake"] == "New Leads"
    assert client.get("/api/kanban").get_json()["labels"]["intake"] == "New Leads"
    assert client.post(
        "/api/kanban/labels", json={"status": "unknown", "label": "Invalid"}
    ).status_code == 400


def test_csv_export_adds_tag_columns(client) -> None:
    import csv
    import io

    lead_id = client.post(
        "/api/lead",
        json={"email": "export@example.com", "first_name": "Export", "lead_source": "manual"},
    ).get_json()["lead_id"]
    assert client.patch(
        f"/api/lead/{lead_id}/tags",
        json={
            "industry": "Technology",
            "lead_source": "Website",
            "custom": ["VIP", "Technology", "follow-up"],
        },
    ).status_code == 200

    response = client.get("/api/leads/export/csv")
    assert response.status_code == 200
    reader = csv.DictReader(io.StringIO(response.data.decode("utf-8")))
    assert {"Industry", "Lead Source", "Tags"} <= set(reader.fieldnames)
    row = next(reader)
    assert row["Industry"] == "Technology"
    assert row["Lead Source"] == "Website"
    assert row["Tags"] == "VIP"
    assert row["lead_source"] == "manual"


def test_admin_can_add_tag_options(client) -> None:
    import dashboard

    assert client.get("/api/tags/options").get_json() == {
        "industries": [],
        "lead_sources": [],
    }

    response = client.post(
        "/api/tags/options", json={"kind": "industries", "value": "Aerospace"}
    )
    assert response.status_code == 200
    assert "Aerospace" in client.get("/api/tags/options").get_json()["industries"]

    assert client.post(
        "/api/users",
        json={
            "username": "viewer",
            "password": "viewer-pass",
            "display_name": "Viewer",
            "role": "user",
        },
    ).status_code == 201
    viewer = dashboard.app.test_client()
    assert viewer.post(
        "/login", data={"username": "viewer", "password": "viewer-pass"}
    ).status_code in (200, 302)
    assert viewer.post(
        "/api/tags/options", json={"kind": "industries", "value": "Retail"}
    ).status_code == 403
