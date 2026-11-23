"""Smoke tests for the serverless health and cron endpoints."""

import os

CRON_HEADERS = {"Authorization": f"Bearer {os.environ['CRON_SECRET']}"}


def test_health_endpoint(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_cron_requires_bearer_secret(client):
    assert client.get("/api/cron/process-sequences").status_code == 401


def test_cron_sequence_and_campaign_runs_are_bounded(client):
    sequences = client.get("/api/cron/process-sequences", headers=CRON_HEADERS)
    assert sequences.status_code == 200
    assert sequences.get_json()["sent"] == 0

    campaigns = client.get("/api/cron/process-campaigns", headers=CRON_HEADERS)
    assert campaigns.status_code == 200
    assert campaigns.get_json()["total"] == 0
