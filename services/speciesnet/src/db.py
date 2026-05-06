"""SQLite persistence for species classifications.

The speciesnet service is the sole writer.  The web service reads this
DB read-only when rendering the events list, joining on
``feedback_token`` to attach a species label to each detection row.

A separate database (``/data/speciesnet.db``, configurable via
``SPECIES_DB_PATH``) preserves the project's "single writer per DB"
design rule — ``scarguard.db`` is owned exclusively by the detector +
web's feedback writes.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_PATH: str = os.environ.get("SPECIES_DB_PATH", "/data/speciesnet.db")

_lock = threading.Lock()
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local connection (created on first use per thread)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db() -> None:
    """Create the species_classifications table + indexes if missing."""
    with _lock:
        conn = _get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS species_classifications (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_token  TEXT    NOT NULL UNIQUE,
                camera_name     TEXT    NOT NULL,
                detection_class TEXT    NOT NULL,
                status          TEXT    NOT NULL,
                common_name     TEXT,
                species         TEXT,
                genus           TEXT,
                family          TEXT,
                "order"         TEXT,
                class           TEXT,
                score           REAL,
                geofenced       INTEGER,
                top_predictions TEXT,
                error           TEXT,
                created_at      TEXT NOT NULL,
                completed_at    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_species_token
                ON species_classifications(feedback_token);
            CREATE INDEX IF NOT EXISTS idx_species_created
                ON species_classifications(created_at);
        """)
        conn.commit()


def insert_pending(
    feedback_token: str,
    camera_name: str,
    detection_class: str,
) -> None:
    """Record that classification has started for *feedback_token*.

    A pending row exists from the moment the service decides to classify
    until the prediction lands.  It lets the web UI render a
    "classifying…" badge instead of a blank cell during the 60-180s
    Lambda cold start window.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO species_classifications
                    (feedback_token, camera_name, detection_class, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (feedback_token, camera_name, detection_class, now),
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception("Failed to insert pending species row for %s", feedback_token)


def update_success(
    feedback_token: str,
    *,
    common_name: str,
    species: str,
    genus: str,
    family: str,
    order: str,
    class_: str,
    score: float,
    geofenced: bool,
    top_predictions: list[dict],
) -> None:
    """Record a successful classification for *feedback_token*."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """
                UPDATE species_classifications
                   SET status = 'success',
                       common_name = ?,
                       species = ?,
                       genus = ?,
                       family = ?,
                       "order" = ?,
                       class = ?,
                       score = ?,
                       geofenced = ?,
                       top_predictions = ?,
                       completed_at = ?,
                       error = NULL
                 WHERE feedback_token = ?
                """,
                (
                    common_name, species, genus, family, order, class_,
                    score, 1 if geofenced else 0,
                    json.dumps(top_predictions),
                    now,
                    feedback_token,
                ),
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception("Failed to record species success for %s", feedback_token)


def update_failure(
    feedback_token: str,
    *,
    status: str,
    error: str,
) -> None:
    """Record an error or timeout for *feedback_token*.

    *status* should be ``'error'`` for HTTP/network failures or
    ``'timeout'`` when polling budget was exhausted.  Both end the
    "classifying…" UI state.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                """
                UPDATE species_classifications
                   SET status = ?, error = ?, completed_at = ?
                 WHERE feedback_token = ?
                """,
                (status, error[:500], now, feedback_token),
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception("Failed to record species failure for %s", feedback_token)


def get_by_tokens(tokens: list[str]) -> dict[str, sqlite3.Row]:
    """Return a ``{feedback_token: row}`` map for read-only callers (web service).

    Empty input returns an empty dict.  Tokens with no row are simply
    absent from the result; the caller falls back to "no classification
    yet".
    """
    if not tokens:
        return {}
    placeholders = ",".join("?" * len(tokens))
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            f"""
            SELECT feedback_token, status, common_name, species, score,
                   geofenced, error
            FROM species_classifications
            WHERE feedback_token IN ({placeholders})
            """,
            tokens,
        ).fetchall()
    return {r["feedback_token"]: r for r in rows}
