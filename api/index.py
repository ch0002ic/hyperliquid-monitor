"""Vercel serverless entrypoint exposing the FastAPI application."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api import app as fastapi_app  # noqa: E402

app = fastapi_app

__all__ = ["app"]
