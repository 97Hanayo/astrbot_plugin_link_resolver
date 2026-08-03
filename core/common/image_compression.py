"""Helpers for keeping generated image files within message size limits."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_IMAGE_HEIGHT = 10000
_MIN_JPEG_QUALITY = 45
_JPEG_QUALITIES = (85, 80, 75, 70, 64, 58, 52, _MIN_JPEG_QUALITY)


def fit_image_file_limits(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    max_height: int = DEFAULT_MAX_IMAGE_HEIGHT,
    force_jpeg: bool = False,
) -> Path:
    """Resize/compress an image file and return the path that should be sent.

    PNG is kept when it fits the limits unless ``force_jpeg`` is enabled. If
    JPEG is selected, it is progressively reduced until it fits the byte limit
    or reaches a conservative minimum size.
    """
    if max_bytes <= 0 and max_height <= 0:
        return path
    if not path.exists():
        return path

    with Image.open(path) as image:
        normalized = image.convert("RGB")

    try:
        normalized = _resize_to_height(normalized, max_height)
        if not force_jpeg and _save_png_if_small(normalized, path, max_bytes):
            return path
        return _save_jpeg_with_limits(normalized, path, max_bytes, max_height)
    finally:
        normalized.close()


def _resize_to_height(image: Image.Image, max_height: int) -> Image.Image:
    if max_height <= 0 or image.height <= max_height:
        return image
    width = max(1, round(image.width * max_height / image.height))
    resized = image.resize((width, max_height), Image.Resampling.LANCZOS)
    image.close()
    return resized


def _save_png_if_small(image: Image.Image, path: Path, max_bytes: int) -> bool:
    image.save(path, format="PNG", optimize=True)
    return max_bytes <= 0 or path.stat().st_size <= max_bytes


def _save_jpeg_with_limits(
    image: Image.Image,
    source_path: Path,
    max_bytes: int,
    max_height: int,
) -> Path:
    jpeg_path = source_path.with_suffix(".jpg")
    working = image.copy()
    try:
        scale = 0.9
        while True:
            for quality in _JPEG_QUALITIES:
                working.save(
                    jpeg_path,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )
                if max_bytes <= 0 or jpeg_path.stat().st_size <= max_bytes:
                    _remove_if_different(source_path, jpeg_path)
                    return jpeg_path
            if working.width <= 360 or working.height <= 360:
                _remove_if_different(source_path, jpeg_path)
                return jpeg_path
            next_width = max(360, round(working.width * scale))
            next_height = max(360, round(working.height * scale))
            if max_height > 0:
                next_height = min(next_height, max_height)
            resized = working.resize((next_width, next_height), Image.Resampling.LANCZOS)
            working.close()
            working = resized
            scale = 0.85
    finally:
        working.close()


def _remove_if_different(path: Path, keep_path: Path) -> None:
    if path == keep_path:
        return
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        logger.debug("Image cleanup skipped for %s: %s", path, exc)


__all__ = [
    "DEFAULT_MAX_IMAGE_BYTES",
    "DEFAULT_MAX_IMAGE_HEIGHT",
    "fit_image_file_limits",
]
