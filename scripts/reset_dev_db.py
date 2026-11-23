#!/usr/bin/env python3
"""Destructively reset a local development PostgreSQL database.

This script is deliberately explicit and is never used by CI or by the
application. It refuses URLs that do not point to a local/Compose host and
requires --force.

Usage:
    python scripts/reset_dev_db.py --force
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from database import reset_database  # noqa: E402
from db.session import get_database_url, mask_database_url, redact_credentials  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres"}


def _assert_dev_database(url: str) -> None:
    """Fail closed unless the target looks like a local/dev database."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    database = parsed.path.lstrip("/")

    if host not in LOCAL_HOSTS:
        raise RuntimeError(
            f"Refusing to reset non-local database host {host!r}. "
            "Use your provider's dashboard for managed databases."
        )
    if not (database.startswith("opencrm") or "test" in database.lower()):
        raise RuntimeError(
            f"Refusing to reset database {database!r}; expected an 'opencrm' "
            "or 'test' database name."
        )


def reset_dev_database(force: bool = False) -> bool:
    """Downgrade to base and re-apply all migrations."""
    url = get_database_url()
    _assert_dev_database(url)

    if not force:
        print("[CANCELLED] Pass --force to confirm that all local data may be deleted.")
        return False

    old_cwd = os.getcwd()
    try:
        os.chdir(ROOT)
        print(f"[RESET] Target: {mask_database_url(url)}")
        print("[RESET] Running: alembic downgrade base")
        command.downgrade(Config(str(ROOT / "alembic.ini")), "base")
        print("[RESET] Running: alembic upgrade head")
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        print("[SUCCESS] Local development database reset complete.")
        return True
    finally:
        os.chdir(old_cwd)
        reset_database()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="Confirm deletion of all local data"
    )
    args = parser.parse_args()
    try:
        sys.exit(0 if reset_dev_database(force=args.force) else 1)
    except Exception as exc:  # noqa: BLE001 - CLI reports all failures
        print(f"[ERROR] Reset failed: {redact_credentials(exc)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
