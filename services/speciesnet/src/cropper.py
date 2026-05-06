"""Crop a detection bbox out of a saved snapshot, with optional padding.

The detector saves clean (un-annotated) snapshots and stores the bbox in
``[x1, y1, x2, y2]`` pixel coordinates.  SpeciesNet performs better when
it sees a small margin of context around the animal (feathers, tail,
posture cues), so we expand the bbox by a configurable percentage of the
bbox dimensions before cropping, clamped to the frame.
"""

from __future__ import annotations

import io

from PIL import Image


def expand_bbox(
    bbox: tuple[int, int, int, int],
    frame_w: int,
    frame_h: int,
    padding_pct: float,
) -> tuple[int, int, int, int]:
    """Expand *bbox* by *padding_pct* of its width/height, clamped to the frame.

    *padding_pct* of 0.10 adds 10% of bbox width on each side and 10% of
    bbox height on top and bottom.  The result is always within
    ``[0, frame_w] x [0, frame_h]`` so the caller can crop without
    additional bounds checking.
    """
    x1, y1, x2, y2 = bbox
    if x2 < x1 or y2 < y1:
        raise ValueError(f"Invalid bbox: {bbox!r}")
    w = x2 - x1
    h = y2 - y1
    pad_x = int(round(w * padding_pct))
    pad_y = int(round(h * padding_pct))
    nx1 = max(0, x1 - pad_x)
    ny1 = max(0, y1 - pad_y)
    nx2 = min(frame_w, x2 + pad_x)
    ny2 = min(frame_h, y2 + pad_y)
    if nx2 <= nx1 or ny2 <= ny1:
        raise ValueError(
            f"bbox collapsed after expansion: {bbox!r} in {frame_w}x{frame_h}"
        )
    return nx1, ny1, nx2, ny2


def crop_to_jpeg(
    snapshot_path: str,
    bbox: tuple[int, int, int, int],
    padding_pct: float = 0.10,
    quality: int = 90,
) -> bytes:
    """Open *snapshot_path*, crop to *bbox* (with padding), return JPEG bytes.

    The classifier expects a square-ish RGB JPEG.  We preserve aspect
    ratio — SpeciesNet's preprocessing handles its own resize-to-480.
    """
    with Image.open(snapshot_path) as img:
        rgb = img.convert("RGB")
        expanded = expand_bbox(bbox, rgb.width, rgb.height, padding_pct)
        cropped = rgb.crop(expanded)
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
