"""Tests for the species_classifications SQLite layer."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def species_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reload the db module against an isolated, per-test SQLite file."""
    monkeypatch.setenv("SPECIES_DB_PATH", str(tmp_path / "species.db"))
    import db as _db
    importlib.reload(_db)
    _db.init_db()
    return _db


class TestPendingFlow:
    def test_pending_then_success(self, species_db) -> None:
        species_db.insert_pending("tok-1", "pond-north", "bird")
        species_db.update_success(
            "tok-1",
            common_name="Great Blue Heron",
            species="Ardea herodias",
            genus="Ardea",
            family="Ardeidae",
            order="Pelecaniformes",
            class_="Aves",
            score=0.87,
            geofenced=False,
            top_predictions=[{"common_name": "Great Blue Heron", "score": 0.87}],
        )
        rows = species_db.get_by_tokens(["tok-1"])
        assert "tok-1" in rows
        row = rows["tok-1"]
        assert row["status"] == "success"
        assert row["common_name"] == "Great Blue Heron"
        assert row["score"] == pytest.approx(0.87)
        assert row["geofenced"] == 0

    def test_pending_then_timeout(self, species_db) -> None:
        species_db.insert_pending("tok-2", "pond-north", "bird")
        species_db.update_failure("tok-2", status="timeout", error="3 minutes elapsed")
        row = species_db.get_by_tokens(["tok-2"])["tok-2"]
        assert row["status"] == "timeout"
        assert "3 minutes" in row["error"]

    def test_duplicate_pending_is_noop(self, species_db) -> None:
        # INSERT OR IGNORE means a duplicate detection event (same token)
        # never crashes the worker.
        species_db.insert_pending("tok-3", "cam", "bird")
        species_db.insert_pending("tok-3", "cam", "bird")
        rows = species_db.get_by_tokens(["tok-3"])
        assert len(rows) == 1


class TestGetByTokens:
    def test_empty_input_returns_empty(self, species_db) -> None:
        assert species_db.get_by_tokens([]) == {}

    def test_unknown_token_absent_from_result(self, species_db) -> None:
        species_db.insert_pending("tok-known", "cam", "bird")
        rows = species_db.get_by_tokens(["tok-known", "tok-missing"])
        assert "tok-known" in rows
        assert "tok-missing" not in rows
