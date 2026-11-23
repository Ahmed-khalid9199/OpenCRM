#!/usr/bin/env python3
"""Apply Alembic migrations and validate the consolidated PostgreSQL schema.

This replaces the former SQLite schema creator.  Schema ownership now belongs
to Alembic; this script is a convenience wrapper for local development and
operations.  It never creates or modifies a SQLite file.

Usage:
    python scripts/init_db.py
    python scripts/init_db.py --seed-test-data
    python scripts/init_db.py --stats-only
    python scripts/init_db.py --verbose

For a destructive local reset, use scripts/reset_dev_db.py --force instead.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import inspect  # noqa: E402

from config import ConfigurationError  # noqa: E402
from database import DatabaseError, get_database, reset_database  # noqa: E402
from db.models import Base  # noqa: E402
from db.session import (  # noqa: E402
    get_database_url,
    get_engine,
    mask_database_url,
    redact_credentials,
)

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Configure console logging for the CLI."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def validate_database_schema(db: Any = None) -> bool:
    """Validate every migrated table, column and lead index via inspection."""
    print("\n[VALIDATION] Checking PostgreSQL schema...")
    try:
        engine = get_engine()
        with engine.connect() as connection:
            inspector = inspect(connection)
            actual_tables = set(inspector.get_table_names())
            expected_tables = set(Base.metadata.tables)

            missing_tables = expected_tables - actual_tables
            if missing_tables:
                print(f"[ERROR] Missing tables: {sorted(missing_tables)}")
                return False
            print(f"[OK] All {len(expected_tables)} domain tables exist")

            for table_name, table in Base.metadata.tables.items():
                actual_columns = {
                    column["name"] for column in inspector.get_columns(table_name)
                }
                expected_columns = {column.name for column in table.columns}
                missing_columns = expected_columns - actual_columns
                if missing_columns:
                    print(
                        f"[ERROR] Table {table_name} is missing columns: "
                        f"{sorted(missing_columns)}"
                    )
                    return False
            print("[OK] All migrated columns exist")

            lead_indexes = {index["name"] for index in inspector.get_indexes("leads")}
            expected_indexes = {
                "idx_lead_id",
                "idx_status",
                "idx_email",
                "idx_created_time",
                "idx_status_created",
            }
            missing_indexes = expected_indexes - lead_indexes
            if missing_indexes:
                print(f"[WARNING] Missing lead indexes: {sorted(missing_indexes)}")
            else:
                print("[OK] Lead indexes exist")

        print("[SUCCESS] Database schema validation complete!")
        return True
    except Exception as exc:  # noqa: BLE001 - CLI reports all validation failures
        message = redact_credentials(exc)
        print(f"[ERROR] Schema validation failed: {message}")
        logger.error("Schema validation error: %s", message)
        return False


def seed_test_data(db: Any, count: int = 5) -> bool:
    """Seed a small number of clearly labelled development leads."""
    print(f"\n[SEED] Inserting {count} test leads...")
    now = datetime.now(timezone.utc).isoformat()
    test_leads = [
        {
            "lead_id": f"test_lead_{i}",
            "form_id": "test_form_001",
            "created_time": now,
            "email": f"test{i}@example.com",
            "full_name": f"Test User {i}",
            "first_name": "Test",
            "last_name": f"User{i}",
            "phone_number": f"+1555000{i:04d}",
            "company_name": "Test Company",
            "job_title": "Test Manager",
            "raw_field_data": {
                "email": f"test{i}@example.com",
                "full_name": f"Test User {i}",
            },
            "status": "intake" if i % 2 == 0 else "emailed",
        }
        for i in range(1, count + 1)
    ]

    inserted = 0
    skipped = 0
    for lead in test_leads:
        try:
            if db.insert_lead(lead) is not None:
                inserted += 1
                print(f"[OK] Inserted test lead: {lead['email']}")
            else:
                skipped += 1
                print(f"[SKIP] Lead already exists: {lead['email']}")
        except Exception as exc:  # noqa: BLE001 - report each seed failure
            skipped += 1
            print(
                f"[ERROR] Failed to insert lead {lead['email']}: "
                f"{redact_credentials(exc)}"
            )

    print(f"\n[SEED] Results: {inserted} inserted, {skipped} failed/skipped")
    return inserted > 0


def print_database_stats(db: Any) -> None:
    """Print the repository statistics in a compact CLI report."""
    print("\n" + "=" * 80)
    print("DATABASE STATISTICS")
    print("=" * 80)
    try:
        stats: Dict[str, Any] = db.get_stats()
        print(f"\nDatabase URL:  {stats.get('database_path', mask_database_url(get_database_url()))}")
        print(f"Total Leads:   {stats.get('total_leads', 0)}")
        print(f"Recent (24h):  {stats.get('recent_24h', 0)}")

        breakdown = stats.get("status_breakdown", {})
        if breakdown:
            print("\nStatus Breakdown:")
            for status, count in breakdown.items():
                print(f"  {status:15} {count:>5}")
        else:
            print("\nNo leads in database yet.")
        print("\n" + "=" * 80)
    except Exception as exc:  # noqa: BLE001 - CLI reports stats failures
        message = redact_credentials(exc)
        print(f"[ERROR] Failed to get database stats: {message}")
        logger.error("Stats error: %s", message)


def initialize_database(
    seed_data: bool = False,
    test_data_count: int = 5,
    verbose: bool = False,
) -> bool:
    """Run Alembic, validate the schema, optionally seed data, print stats."""
    print("\n" + "=" * 80)
    print("ARKITEKT OPENCRM - POSTGRESQL DATABASE INITIALIZATION")
    print("=" * 80)

    old_cwd = os.getcwd()
    try:
        os.chdir(ROOT)
        print("\n[INIT] Applying Alembic migrations...")
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        print(f"[SUCCESS] Schema is up to date at: {mask_database_url(get_database_url())}")

        db = get_database()
        if not validate_database_schema(db):
            return False

        if seed_data and not seed_test_data(db, count=test_data_count):
            print("\n[WARNING] No test data was inserted")

        print_database_stats(db)
        print("\n[SUCCESS] Database initialization complete!")
        print("=" * 80 + "\n")
        return True
    except ConfigurationError as exc:
        print(f"\n[ERROR] Configuration error: {redact_credentials(exc)}")
        print("\nPlease ensure .env contains DATABASE_URL and all required values.")
        return False
    except DatabaseError as exc:
        message = redact_credentials(exc)
        print(f"\n[ERROR] Database error: {message}")
        logger.error("Database initialization failed: %s", message)
        return False
    except Exception as exc:  # noqa: BLE001 - CLI reports all failures
        message = redact_credentials(exc)
        print(f"\n[ERROR] Unexpected error: {message}")
        logger.error("Initialization error: %s", message)
        if verbose:
            import traceback

            traceback.print_exc()
        return False
    finally:
        os.chdir(old_cwd)
        reset_database()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Initialize Arkitekt OpenCRM PostgreSQL database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # Apply migrations and validate
  %(prog)s --seed-test-data         # Also insert development leads
  %(prog)s --stats-only             # Print statistics only
  %(prog)s --verbose                # Verbose output

Destructive local reset:
  python scripts/reset_dev_db.py --force
        """,
    )
    parser.add_argument("--seed-test-data", action="store_true", help="Seed development leads")
    parser.add_argument(
        "--test-data-count", type=int, default=5, help="Number of seed leads (default: 5)"
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument(
        "--stats-only", action="store_true", help="Only print database statistics"
    )

    args = parser.parse_args()
    setup_logging(verbose=args.verbose)

    try:
        if args.stats_only:
            print_database_stats(get_database())
            reset_database()
            sys.exit(0)

        success = initialize_database(
            seed_data=args.seed_test_data,
            test_data_count=args.test_data_count,
            verbose=args.verbose,
        )
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n[CANCELLED] Operation cancelled by user")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - CLI reports all failures
        message = redact_credentials(exc)
        print(f"\n[ERROR] Unexpected error: {message}")
        logger.error("Main error: %s", message)
        if args.verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
