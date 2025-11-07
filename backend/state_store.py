"""State storage helpers for sharing monitoring data across environments."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Any, Dict

try:  # pragma: no cover - optional dependency when redis is unused
    import redis  # type: ignore
except ImportError:  # pragma: no cover - redis not installed
    redis = None


logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).resolve().parent / "position_state.json"
_STATE_LOCK = Lock()

_REDIS_URL = os.getenv("STATE_REDIS_URL") or os.getenv("REDIS_URL") or os.getenv("UPSTASH_REDIS_URL")
_REDIS_KEY = os.getenv("STATE_REDIS_KEY", "hyperliquid:position_state")


def _get_redis_client():
    if not _REDIS_URL or redis is None:
        return None
    try:
        # Upstash and most hosted Redis providers offer TLS URLs.
        return redis.from_url(_REDIS_URL, decode_responses=True)
    except Exception as exc:  # pragma: no cover - connection issues at import time
        logger.warning("Failed to initialise Redis client: %%s", exc)
        return None


_REDIS_CLIENT = _get_redis_client()


def load_state_snapshot() -> Dict[str, Any]:
    """Retrieve the persisted position state.

    Preference order: Redis cache (if configured) then local JSON file.
    """
    # Attempt remote cache first
    client = _REDIS_CLIENT
    if client is not None:
        try:
            payload = client.get(_REDIS_KEY)
            if payload:
                return json.loads(payload)
        except Exception as exc:  # pragma: no cover - network failure
            logger.warning("Failed to read state from Redis: %s", exc)

    # Fall back to local JSON file
    with _STATE_LOCK:
        if not _STATE_FILE.exists():
            return {}
        try:
            with _STATE_FILE.open() as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read state file: %s", exc)
            return {}


def save_state_snapshot(state: Dict[str, Any]) -> None:
    """Persist the latest position state to Redis and local disk (best effort)."""
    serialized = json.dumps(state)

    client = _REDIS_CLIENT
    if client is not None:
        try:
            client.set(_REDIS_KEY, serialized)
        except Exception as exc:  # pragma: no cover - network failure
            logger.warning("Failed to write state to Redis: %s", exc)

    with _STATE_LOCK:
        try:
            with _STATE_FILE.open("w") as handle:
                handle.write(serialized)
        except OSError as exc:
            logger.error("Failed to write state file: %s", exc)
