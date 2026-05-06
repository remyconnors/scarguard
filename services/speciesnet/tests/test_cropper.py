"""Tests for cropper.expand_bbox and crop_to_jpeg."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from cropper import crop_to_jpeg, expand_bbox
from PIL import Image


class TestExpandBbox:
    def test_zero_padding_is_identity(self) -> None:
        assert expand_bbox((100, 100, 200, 200), 640, 480, 0.0) == (100, 100, 200, 200)

    def test_padding_expands_evenly(self) -> None:
        # bbox is 100x100; 10% pad = 10px on each side
        result = expand_bbox((100, 100, 200, 200), 640, 480, 0.10)
        assert result == (90, 90, 210, 210)

    def test_padding_clamps_to_left_edge(self) -> None:
        # bbox at (0, 0) — pad would push x1 negative
        result = expand_bbox((0, 0, 100, 100), 640, 480, 0.50)
        assert result[0] == 0
        assert result[1] == 0

    def test_padding_clamps_to_right_edge(self) -> None:
        # bbox at the right edge — x2 must clamp to frame width
        result = expand_bbox((540, 100, 640, 200), 640, 480, 0.50)
        assert result[2] == 640

    def test_padding_clamps_to_bottom_edge(self) -> None:
        result = expand_bbox((100, 380, 200, 480), 640, 480, 0.50)
        assert result[3] == 480

    def test_invalid_bbox_raises(self) -> None:
        with pytest.raises(ValueError):
            expand_bbox((200, 200, 100, 100), 640, 480, 0.10)


class TestCropToJpeg:
    def test_returns_valid_jpeg_bytes(self, fake_snapshot: Path) -> None:
        out = crop_to_jpeg(str(fake_snapshot), (100, 100, 300, 300), padding_pct=0.0)
        assert isinstance(out, bytes)
        # PIL can re-open it
        with Image.open(io.BytesIO(out)) as img:
            assert img.format == "JPEG"
            assert img.size == (200, 200)

    def test_padding_grows_output(self, fake_snapshot: Path) -> None:
        # 200x200 bbox + 10% pad → 240x240 cropped
        out = crop_to_jpeg(str(fake_snapshot), (100, 100, 300, 300), padding_pct=0.10)
        with Image.open(io.BytesIO(out)) as img:
            assert img.size == (240, 240)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            crop_to_jpeg(str(tmp_path / "nope.jpg"), (0, 0, 10, 10))
