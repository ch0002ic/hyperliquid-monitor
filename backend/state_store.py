"""State storage helpers for sharing monitoring data across environments."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Optional, cast

try:  # pragma: no cover - optional dependency when redis is unused
    import redis  # type: ignore
except ImportError:  # pragma: no cover - redis not installed
    redis = None


logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).resolve().parent / "position_state.json"
_STATE_LOCK = Lock()

_REDIS_URL: Optional[str] = None
_REDIS_KEY = "hyperliquid:position_state"

AlertHandler = Callable[[str], None]
_ALERT_HANDLER: Optional[AlertHandler] = None
_REDIS_ALERT_FIRED = False


def _get_redis_client(url: Optional[str]):
    if not url or redis is None:
        return None
    try:
        # Upstash and most hosted Redis providers offer TLS URLs.
        return redis.from_url(url, decode_responses=True)
    except Exception as exc:  # pragma: no cover - connection issues at import time
        logger.warning("Failed to initialise Redis client: %%s", exc)
        return None


_REDIS_CLIENT = None


def register_state_store_alert_handler(handler: Optional[AlertHandler]) -> None:
    """Register a callback invoked the first time Redis persistence fails."""

    global _ALERT_HANDLER
    _ALERT_HANDLER = handler


def _notify_redis_issue(message: str) -> None:
    global _REDIS_ALERT_FIRED
    if not _REDIS_URL:
        return
    if not _ALERT_HANDLER:
        return
    if _REDIS_ALERT_FIRED:
        return
    try:
        _ALERT_HANDLER(message)
    except Exception as exc:  # pragma: no cover - defensive alert handling
        logger.warning("State store alert handler failed: %s", exc)
    finally:
        _REDIS_ALERT_FIRED = True


def _mark_redis_healthy() -> None:
    global _REDIS_ALERT_FIRED
    if _REDIS_ALERT_FIRED:
        _REDIS_ALERT_FIRED = False


def _configure_from_env() -> None:
    global _REDIS_URL, _REDIS_KEY, _REDIS_CLIENT, _REDIS_ALERT_FIRED

    env_url = os.getenv("STATE_REDIS_URL") or os.getenv("REDIS_URL") or os.getenv("UPSTASH_REDIS_URL")
    env_key = os.getenv("STATE_REDIS_KEY", "hyperliquid:position_state")

    url_changed = env_url != _REDIS_URL

    _REDIS_URL = env_url
    _REDIS_KEY = env_key

    if url_changed:
        _REDIS_CLIENT = _get_redis_client(_REDIS_URL)
        _REDIS_ALERT_FIRED = False
    elif _REDIS_CLIENT is None and _REDIS_URL:
        _REDIS_CLIENT = _get_redis_client(_REDIS_URL)


def refresh_state_store_configuration() -> None:
    """Refresh Redis connection and key from current environment variables."""

    _configure_from_env()


def load_state_snapshot() -> Dict[str, Any]:
    """Retrieve the persisted position state.

    Preference order: Redis cache (if configured) then local JSON file.
    """
    _configure_from_env()
    # Attempt remote cache first
    client = _REDIS_CLIENT
    if client is not None:
        try:
            payload = client.get(_REDIS_KEY)
            if payload:
                text_payload = payload.decode("utf-8") if isinstance(payload, bytes) else cast(str, payload)
                _mark_redis_healthy()
                return json.loads(text_payload)
        except Exception as exc:  # pragma: no cover - network failure
            logger.warning("Failed to read state from Redis: %s", exc)
            _notify_redis_issue(f"Redis read failed: {exc}")
    elif _REDIS_URL:
        _notify_redis_issue("Redis client unavailable; using local state snapshot")

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
    _configure_from_env()

    serialized = json.dumps(state)

    client = _REDIS_CLIENT
    if client is not None:
        try:
            client.set(_REDIS_KEY, serialized)
            _mark_redis_healthy()
        except Exception as exc:  # pragma: no cover - network failure
            logger.warning("Failed to write state to Redis: %s", exc)
            _notify_redis_issue(f"Redis write failed: {exc}")
    elif _REDIS_URL:
        _notify_redis_issue("Redis client unavailable; wrote state to local file only")

    with _STATE_LOCK:
        try:
            with _STATE_FILE.open("w") as handle:
                handle.write(serialized)
        except OSError as exc:
            logger.error("Failed to write state file: %s", exc)
