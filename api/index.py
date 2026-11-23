"""Vercel serverless entrypoint for the OpenCRM Flask dashboard."""

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from dashboard import app  # noqa: E402

__all__ = ["app"]
