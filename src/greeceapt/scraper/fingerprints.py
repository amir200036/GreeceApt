"""Versioned perceptual fingerprints used by listing entity resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import imagehash
from PIL import Image


FINGERPRINT_VERSION = "phash-v2"


def _parse_hash(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = value if isinstance(value, int) else int(str(value).strip(), 16)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed < 2**64 else None


@dataclass(frozen=True)
class PhotoFingerprint:
    version: str
    full: int | None
    center: int | None

    @classmethod
    def from_json(cls, value: object) -> PhotoFingerprint | None:
        if isinstance(value, str):
            full = _parse_hash(value)
            return None if full is None else cls("phash-v1", full, None)
        if not isinstance(value, dict):
            return None
        full = _parse_hash(value.get("full"))
        if value.get("version") == "phash-v1":
            return None if full is None else cls("phash-v1", full, None)
        if value.get("version") != FINGERPRINT_VERSION:
            return None
        center = _parse_hash(value.get("center"))
        if full is None or center is None:
            return None
        return cls(FINGERPRINT_VERSION, full, center)

    def to_json(self) -> dict[str, str | None]:
        return {
            "version": self.version,
            "full": None if self.full is None else f"{self.full:016x}",
            "center": None if self.center is None else f"{self.center:016x}",
        }

    @property
    def is_complete_v2(self) -> bool:
        return self.version == FINGERPRINT_VERSION and self.full is not None and self.center is not None


def _center_crop(image: Image.Image) -> Image.Image:
    width, height = image.size
    left = int(width * 0.125)
    top = int(height * 0.125)
    right = width - left
    bottom = height - top
    if right <= left:
        left, right = 0, max(1, width)
    if bottom <= top:
        top, bottom = 0, max(1, height)
    return image.crop((left, top, right, bottom))


def fingerprint_image(image: Image.Image) -> PhotoFingerprint:
    rgb = image.convert("RGB")
    full = int(str(imagehash.phash(rgb)), 16)
    center = int(str(imagehash.phash(_center_crop(rgb))), 16)
    return PhotoFingerprint(FINGERPRINT_VERSION, full, center)
