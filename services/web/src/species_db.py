"""Read-only access to the speciesnet sidecar's SQLite DB.

The speciesnet service is the sole writer of ``/data/speciesnet.db``.
The web service reads it to attach species labels to detection rows.
The DB may not exist (sidecar disabled or never run) — in that case we
return empty results so the events page renders normally.
"""

from __future__ import annotations

import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("SPECIES_DB_PATH", "/data/speciesnet.db")


def _connect() -> sqlite3.Connection | None:
    """Open a read-only connection.  Returns None if the DB does not exist."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        # `mode=ro` ensures we never accidentally write from the web service.
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2,
        )
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        logger.warning("Failed to open species DB at %s", DB_PATH, exc_info=True)
        return None


def get_by_tokens(tokens: list[str]) -> dict[str, dict]:
    """Return ``{feedback_token: species_row_dict}`` for the given tokens.

    Tokens with no classification (or when the species DB is missing)
    are simply absent from the result — caller's job to fall back.
    """
    if not tokens:
        return {}
    conn = _connect()
    if conn is None:
        return {}
    placeholders = ",".join("?" * len(tokens))
    try:
        rows = conn.execute(
            f"""
            SELECT feedback_token, status, common_name, species, score,
                   geofenced, error
            FROM species_classifications
            WHERE feedback_token IN ({placeholders})
            """,
            tokens,
        ).fetchall()
    except sqlite3.Error:
        logger.warning("Species DB read failed", exc_info=True)
        conn.close()
        return {}
    conn.close()
    return {r["feedback_token"]: dict(r) for r in rows}
