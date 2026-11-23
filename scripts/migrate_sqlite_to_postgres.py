#!/usr/bin/env python3
"""Migrate the legacy SQLite CRM database into PostgreSQL.

The SQLite source is opened read-only and is never modified.  All target rows
keep their original primary keys, so foreign-key relationships are preserved.
The utility is safely rerunnable: rows whose IDs already exist in PostgreSQL
are skipped and reported.  JSON/text and timestamp columns are converted to
JSONB and TIMESTAMPTZ respectively.

Usage:
    python scripts/migrate_sqlite_to_postgres.py
    python scripts/migrate_sqlite_to_postgres.py --sqlite-path data/leads.db
    python scripts/migrate_sqlite_to_postgres.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Numeric,
    bindparam,
    column,
    create_engine,
    inspect,
    table as sa_table,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.engine import Connection, Engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.session import (  # noqa: E402
    dispose_engine,
    get_database_url,
    mask_database_url,
    redact_credentials,
)


TABLE_ORDER = (
    "leads",
    "users",
    "lead_notes",
    "sequences",
    "sequence_enrollments",
    "campaigns",
    "campaign_sends",
)

JSON_FIELDS: Dict[str, Tuple[str, ...]] = {
    "leads": ("raw_field_data", "tags"),
    "sequences": ("steps",),
    "campaigns": ("lead_filters",),
}

TIMESTAMP_FIELDS: Dict[str, Tuple[str, ...]] = {
    "leads": (
        "created_time",
        "email_sent_at",
        "inserted_at",
        "updated_at",
        "follow_up_date",
        "expected_close_date",
    ),
    "users": ("created_at",),
    "lead_notes": ("created_at",),
    "sequences": ("created_at", "updated_at"),
    "sequence_enrollments": (
        "next_send_at",
        "enrolled_at",
        "updated_at",
    ),
    "campaigns": ("created_at", "updated_at", "sent_at"),
    "campaign_sends": ("sent_at",),
}

TEXT_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "leads": {
        "form_id": "",
        "email": "",
        "full_name": "",
        "first_name": "",
        "last_name": "",
        "phone_number": "",
        "company_name": "",
        "job_title": "",
        "status": "new",
        "lead_source": "meta",
        "assigned_to": "",
    },
    "users": {"display_name": "", "role": "user"},
    "lead_notes": {"author": ""},
    "sequences": {},
    "sequence_enrollments": {"status": "active"},
    "campaigns": {
        "campaign_type": "single",
        "email_provider": "outlook",
        "subject_template": "",
        "body_template": "",
        "status": "draft",
    },
    "campaign_sends": {"status": "pending", "email": ""},
}

INT_DEFAULTS: Dict[str, Dict[str, int]] = {
    "sequence_enrollments": {"current_step": 0},
    "campaigns": {
        "total_recipients": 0,
        "sent_count": 0,
        "failed_count": 0,
    },
}

TARGET_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "leads": (
        "id", "lead_id", "form_id", "created_time", "email", "full_name",
        "first_name", "last_name", "phone_number", "company_name", "job_title",
        "raw_field_data", "status", "email_sent_at", "inserted_at", "updated_at",
        "tags", "lead_source", "follow_up_date", "deal_value",
        "expected_close_date", "assigned_to",
    ),
    "users": ("id", "username", "password", "display_name", "role", "created_at"),
    "lead_notes": ("id", "lead_id", "author", "content", "created_at"),
    "sequences": ("id", "name", "steps", "created_at", "updated_at"),
    "sequence_enrollments": (
        "id", "lead_id", "sequence_id", "current_step", "status", "next_send_at",
        "enrolled_at", "updated_at",
    ),
    "campaigns": (
        "id", "name", "campaign_type", "email_provider", "subject_template",
        "body_template", "lead_filters", "status", "total_recipients",
        "sent_count", "failed_count", "created_at", "updated_at", "sent_at",
    ),
    "campaign_sends": (
        "id", "campaign_id", "lead_id", "email", "status", "sent_at", "error",
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        candidate = str(value).strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_json(value: Any, expected: type) -> Any:
    if value in (None, ""):
        return expected()
    if isinstance(value, expected):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, expected):
            return parsed
    raise ValueError(f"expected {expected.__name__}, got {type(value).__name__}")


def _coerce_row(
    table: str,
    source_row: Dict[str, Any],
    source_columns: Set[str],
) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    for column in TARGET_COLUMNS[table]:
        value = source_row.get(column) if column in source_columns else None

        if column in JSON_FIELDS.get(table, ()):
            expected = list if column == "steps" else dict
            row[column] = _parse_json(value, expected)
            continue

        if column in TIMESTAMP_FIELDS.get(table, ()):
            row[column] = _parse_timestamp(value)
            continue

        if column == "deal_value":
            if value in (None, ""):
                row[column] = None
            else:
                try:
                    row[column] = Decimal(str(value))
                except InvalidOperation as exc:
                    raise ValueError(f"invalid decimal {value!r}") from exc
            continue

        if column in INT_DEFAULTS.get(table, {}):
            row[column] = int(value) if value not in (None, "") else INT_DEFAULTS[table][column]
            continue

        if column in TEXT_DEFAULTS.get(table, {}):
            row[column] = value if value is not None else TEXT_DEFAULTS[table][column]
            continue

        if column in ("id", "sequence_id", "campaign_id"):
            if value in (None, ""):
                raise ValueError(f"{column} is required")
            row[column] = int(value)
            continue

        if column in ("lead_id", "username", "password", "content", "name", "email"):
            if value is None:
                raise ValueError(f"{column} is required")
            row[column] = str(value)
            continue

        row[column] = value

    if table == "leads" and row.get("created_time") is None:
        raise ValueError("created_time is required")
    return row


def _source_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for table in TABLE_ORDER:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        counts[table] = (
            conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] if exists else 0
        )
    return counts


def _target_counts(conn: Connection) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for table in TABLE_ORDER:
        counts[table] = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
    return counts


def _migrate_table(
    target: Connection,
    source: sqlite3.Connection,
    table: str,
    errors: List[str],
) -> Tuple[int, int]:
    source_columns = {
        row[1] for row in source.execute(f'PRAGMA table_info("{table}")').fetchall()
    }
    if not source_columns:
        return 0, 0

    columns = TARGET_COLUMNS[table]
    type_overrides: Dict[str, Any] = {}
    for name in JSON_FIELDS.get(table, ()):
        type_overrides[name] = JSONB()
    if table == "leads":
        type_overrides["deal_value"] = Numeric(14, 2)
    target_table = sa_table(
        table,
        *(column(name, type_overrides[name]) if name in type_overrides else column(name) for name in columns),
    )
    stmt = pg_insert(target_table).values(
        {name: bindparam(name) for name in columns}
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["id"])

    existing_ids = {
        row[0] for row in target.execute(text(f'SELECT id FROM "{table}"')).all()
    }
    inserted = 0
    skipped = 0
    cursor = source.execute(f'SELECT * FROM "{table}" ORDER BY id')
    rows = cursor.fetchall()
    source_names = [item[0] for item in cursor.description]
    for index, raw_row in enumerate(rows):
        source_row = dict(zip(source_names, raw_row))
        try:
            row = _coerce_row(table, source_row, source_columns)
            with target.begin_nested():
                target.execute(stmt, row)
            if row["id"] in existing_ids:
                skipped += 1
            else:
                inserted += 1
                existing_ids.add(row["id"])
        except Exception as exc:  # noqa: BLE001 - report and continue per row
            skipped += 1
            errors.append(
                f"{table} row id={source_row.get('id')}: {redact_credentials(exc)}"
            )
    return inserted, skipped


def _reset_sequences(target: Connection) -> None:
    for table in TABLE_ORDER:
        target.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                f"(SELECT COUNT(*) > 0 FROM {table}))"
            )
        )


def _validate(target: Connection, source: sqlite3.Connection) -> List[str]:
    problems: List[str] = []
    for table in TABLE_ORDER:
        source_ids = {
            row[0]
            for row in source.execute(f'SELECT id FROM "{table}"').fetchall()
        }
        if not source_ids:
            continue
        target_ids = {
            row[0]
            for row in target.execute(text(f'SELECT id FROM "{table}"')).all()
        }
        missing = source_ids - target_ids
        if missing:
            problems.append(f"{table}: missing migrated IDs {sorted(missing)[:10]}")

    fk_checks = {
        "lead_notes": "SELECT COUNT(*) FROM lead_notes n LEFT JOIN leads l ON l.lead_id=n.lead_id WHERE l.lead_id IS NULL",
        "sequence_enrollments": (
            "SELECT COUNT(*) FROM sequence_enrollments se "
            "LEFT JOIN leads l ON l.lead_id=se.lead_id "
            "LEFT JOIN sequences s ON s.id=se.sequence_id "
            "WHERE l.lead_id IS NULL OR s.id IS NULL"
        ),
        "campaign_sends": (
            "SELECT COUNT(*) FROM campaign_sends cs "
            "LEFT JOIN campaigns c ON c.id=cs.campaign_id "
            "LEFT JOIN leads l ON l.lead_id=cs.lead_id "
            "WHERE c.id IS NULL OR l.lead_id IS NULL"
        ),
    }
    for name, query in fk_checks.items():
        orphans = target.execute(text(query)).scalar_one()
        if orphans:
            problems.append(f"{name}: {orphans} orphaned rows")

    for table, fields in JSON_FIELDS.items():
        for field in fields:
            invalid = target.execute(
                text(f'SELECT COUNT(*) FROM "{table}" WHERE "{field}" IS NULL')
            ).scalar_one()
            if invalid:
                problems.append(f"{table}.{field}: {invalid} NULL values")
    return problems


def _print_report(
    source_path: Path,
    database_url: str,
    source_counts: Dict[str, int],
    target_counts: Dict[str, int],
    inserted: Dict[str, int],
    skipped: Dict[str, int],
    errors: Sequence[str],
    validation: Sequence[str],
    source_hash_before: str,
    source_hash_after: str,
) -> None:
    print("\n" + "=" * 78)
    print("SQLite -> PostgreSQL migration report")
    print("=" * 78)
    print(f"Source:          {source_path}")
    print(f"Source SHA-256:  {source_hash_before}")
    print(f"Target:          {mask_database_url(database_url)}")
    print()
    print(f"{'Table':<24}{'SQLite':>9}{'Postgres':>11}{'Inserted':>11}{'Skipped':>10}")
    print("-" * 78)
    for table in TABLE_ORDER:
        print(
            f"{table:<24}{source_counts[table]:>9}{target_counts[table]:>11}"
            f"{inserted[table]:>11}{skipped[table]:>10}"
        )
    print("-" * 78)
    print(f"Row errors:      {len(errors)}")
    for error in errors[:50]:
        print(f"  - {error}")
    if len(errors) > 50:
        print(f"  ... and {len(errors) - 50} more")
    print(f"Validation issues: {len(validation)}")
    for issue in validation[:50]:
        print(f"  - {issue}")
    print(f"Source unchanged: {source_hash_before == source_hash_after}")
    print("=" * 78)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite-path",
        default=os.getenv("SQLITE_MIGRATION_SOURCE", os.getenv("DB_PATH", "data/leads.db")),
        help="Read-only SQLite source database (default: data/leads.db)",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", ""),
        help="PostgreSQL target URL (default: DATABASE_URL)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report source counts only")
    parser.add_argument(
        "--no-alembic", action="store_true", help="Do not run Alembic upgrade head"
    )
    args = parser.parse_args(argv)

    source_path = Path(args.sqlite_path)
    if not source_path.is_absolute():
        source_path = ROOT / source_path
    if not source_path.is_file():
        parser.error(f"SQLite source not found: {source_path}")

    database_url = args.database_url.strip()
    if not database_url:
        parser.error("DATABASE_URL is required (or pass --database-url)")
    os.environ["DATABASE_URL"] = database_url

    source_hash_before = _sha256(source_path)
    source_uri = source_path.resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    source.row_factory = sqlite3.Row

    inserted = {table: 0 for table in TABLE_ORDER}
    skipped = {table: 0 for table in TABLE_ORDER}
    errors: List[str] = []
    validation: List[str] = []
    target_counts: Dict[str, int] = {}

    engine: Optional[Engine] = None
    try:
        source_counts = _source_counts(source)
        print(f"Read-only SQLite source: {source_path}")
        print("Source row counts:", source_counts)

        if args.dry_run:
            print("Dry run: no changes made.")
            return 0

        if not args.no_alembic:
            alembic_config = Config(str(ROOT / "alembic.ini"))
            os.chdir(ROOT)
            command.upgrade(alembic_config, "head")

        engine = create_engine(database_url.replace("postgresql://", "postgresql+psycopg://"))
        with engine.begin() as target:
            existing = set(inspect(target).get_table_names())
            missing = set(TABLE_ORDER) - existing
            if missing:
                raise RuntimeError(f"Missing target tables: {sorted(missing)}")

            for table in TABLE_ORDER:
                ins, skip = _migrate_table(target, source, table, errors)
                inserted[table] = ins
                skipped[table] = skip
                print(f"Migrated {table:<24} inserted={ins:<6} skipped={skip}")

            _reset_sequences(target)
            target_counts = _target_counts(target)
            validation = _validate(target, source)

        source_hash_after = _sha256(source_path)
        if source_hash_before != source_hash_after:
            validation.append("SQLite source file changed during migration")

        _print_report(
            source_path,
            database_url,
            source_counts,
            target_counts,
            inserted,
            skipped,
            errors,
            validation,
            source_hash_before,
            source_hash_after,
        )
        return 1 if errors or validation else 0
    except Exception as exc:  # noqa: BLE001 - CLI reports all failures
        print(f"[ERROR] Migration failed: {redact_credentials(exc)}", file=sys.stderr)
        return 1
    finally:
        source.close()
        if engine is not None:
            engine.dispose()
        dispose_engine()


if __name__ == "__main__":
    raise SystemExit(main())
