"""Run Alembic migrations during the Vercel build step.

The production URL is injected by Vercel from the configured DATABASE_URL
environment variable. Credentials are masked in all output.
"""

import os
import subprocess
import sys

from db.session import get_database_url, mask_database_url, redact_credentials


def main() -> int:
    url = get_database_url()
    print(f"Running Alembic migrations against {mask_database_url(url)}", flush=True)

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(redact_credentials(result.stdout), end="", flush=True)
    if result.stderr:
        print(redact_credentials(result.stderr), end="", file=sys.stderr, flush=True)
    if result.returncode != 0:
        print("Alembic migration failed; deployment will not continue.", file=sys.stderr, flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
