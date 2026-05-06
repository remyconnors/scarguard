"""Shared fixtures for the speciesnet test suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def fake_snapshot(tmp_path: Path) -> Path:
    """A 640x480 solid-color JPEG that can be safely cropped in tests."""
    img = Image.new("RGB", (640, 480), color=(80, 120, 160))
    path = tmp_path / "snapshot.jpg"
    img.save(path, format="JPEG", quality=80)
    return path
