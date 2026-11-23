"""End-to-end tests for the read-only SQLite -> PostgreSQL migration utility."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

from scripts.migrate_sqlite_to_postgres import main

SCHEMA = """
CREATE TABLE leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT UNIQUE NOT NULL,
    form_id TEXT,
    created_time TEXT NOT NULL,
    email TEXT,
    full_name TEXT,
    first_name TEXT,
    last_name TEXT,
    phone_number TEXT,
    company_name TEXT,
    job_title TEXT,
    raw_field_data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    email_sent_at TEXT,
    inserted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tags TEXT DEFAULT '{}',
    lead_source TEXT DEFAULT 'meta',
    follow_up_date TEXT,
    deal_value REAL,
    expected_close_date TEXT,
    assigned_to TEXT DEFAULT ''
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE lead_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lead_id) REFERENCES leads(lead_id) ON DELETE CASCADE
);
CREATE TABLE sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    steps TEXT NOT NULL DEFAULT '[]',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE sequence_enrollments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT NOT NULL,
    sequence_id INTEGER NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    next_send_at TEXT,
    enrolled_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lead_id) REFERENCES leads(lead_id) ON DELETE CASCADE,
    FOREIGN KEY (sequence_id) REFERENCES sequences(id) ON DELETE CASCADE
);
CREATE TABLE campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    campaign_type TEXT NOT NULL DEFAULT 'single',
    email_provider TEXT NOT NULL DEFAULT 'outlook',
    subject_template TEXT NOT NULL DEFAULT '',
    body_template TEXT NOT NULL DEFAULT '',
    lead_filters TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    total_recipients INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT
);
CREATE TABLE campaign_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    lead_id TEXT NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    sent_at TEXT,
    error TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
    FOREIGN KEY (lead_id) REFERENCES leads(lead_id) ON DELETE CASCADE
);
"""


def _build_source(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        conn.execute(
            """INSERT INTO leads
               (id, lead_id, created_time, email, full_name, raw_field_data, status,
                inserted_at, updated_at, tags, lead_source, deal_value)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                7,
                "migrated-lead-7",
                now,
                "migrated@example.com",
                "Migrated Lead",
                json.dumps({"source": "sqlite", "nested": {"ok": True}}),
                "qualified",
                now,
                now,
                json.dumps({"industry": "Finance", "custom": ["a"]}),
                "manual",
                1234.56,
            ),
        )
        conn.execute(
            "INSERT INTO users (id, username, password, display_name, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (3, "migrated-admin", "hash", "Migrated Admin", "admin", now),
        )
        conn.execute(
            "INSERT INTO lead_notes (id, lead_id, author, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (5, "migrated-lead-7", "migrated-admin", "Imported note", now),
        )
        conn.execute(
            "INSERT INTO sequences (id, name, steps, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (9, "Imported Sequence", json.dumps([{"subject": "Hi", "body": "There"}]), now, now),
        )
        conn.execute(
            """INSERT INTO sequence_enrollments
               (id, lead_id, sequence_id, current_step, status, next_send_at, enrolled_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (11, "migrated-lead-7", 9, 0, "active", now, now, now),
        )
        conn.execute(
            """INSERT INTO campaigns
               (id, name, lead_filters, status, total_recipients, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (13, "Imported Campaign", json.dumps({"status": "qualified"}), "draft", 1, now, now),
        )
        conn.execute(
            """INSERT INTO campaign_sends
               (id, campaign_id, lead_id, email, status, sent_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (17, 13, "migrated-lead-7", "migrated@example.com", "sent", now),
        )
        conn.commit()
    finally:
        conn.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_migration_preserves_ids_json_timestamps_and_is_rerunnable(
    pg_database: str, tmp_path: Path, capsys
) -> None:
    source = tmp_path / "legacy.db"
    _build_source(source)
    before = _sha256(source)

    rc = main(["--sqlite-path", str(source), "--database-url", pg_database, "--no-alembic"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "Row errors:      0" in captured.out
    assert "Validation issues: 0" in captured.out

    rc = main(["--sqlite-path", str(source), "--database-url", pg_database, "--no-alembic"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "inserted=0" in captured.out

    assert _sha256(source) == before

    engine = create_engine(pg_database)
    try:
        with engine.connect() as connection:
            expected = {
                "leads": 1,
                "users": 1,
                "lead_notes": 1,
                "sequences": 1,
                "sequence_enrollments": 1,
                "campaigns": 1,
                "campaign_sends": 1,
            }
            for table, count in expected.items():
                assert connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == count

            lead = connection.execute(
                text(
                    "SELECT id, raw_field_data, tags, deal_value, created_time "
                    "FROM leads WHERE lead_id = 'migrated-lead-7'"
                )
            ).one()
            assert lead.id == 7
            assert lead.raw_field_data["nested"]["ok"] is True
            assert lead.tags["industry"] == "Finance"
            assert float(lead.deal_value) == 1234.56
            assert lead.created_time.tzinfo is not None

            assert connection.execute(text("SELECT id FROM lead_notes")).scalar_one() == 5
            assert connection.execute(text("SELECT id FROM sequences")).scalar_one() == 9
            assert connection.execute(text("SELECT id FROM sequence_enrollments")).scalar_one() == 11
            assert connection.execute(text("SELECT id FROM campaigns")).scalar_one() == 13
            assert connection.execute(text("SELECT id FROM campaign_sends")).scalar_one() == 17

            # Sequence counters must continue after preserved IDs.
            next_id = connection.execute(text("SELECT nextval('leads_id_seq')")).scalar_one()
            assert next_id == 8
    finally:
        engine.dispose()
