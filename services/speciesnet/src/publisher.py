"""Publish species-classification results back onto Redis.

The web service subscribes to ``scarguard:species`` and patches the
matching event row live, so users see "Classifying…" replaced with
"American Crow — 87%" without a page reload.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis as redis_lib

logger = logging.getLogger(__name__)

CHANNEL = "scarguard:species"


def publish(client: redis_lib.Redis, payload: dict[str, Any]) -> None:
    """Best-effort publish — Redis flakes shouldn't take down the service."""
    try:
        client.publish(CHANNEL, json.dumps(payload))
    except redis_lib.RedisError:
        logger.warning("Failed to publish species result", exc_info=True)
