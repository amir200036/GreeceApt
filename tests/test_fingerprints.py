"""Photo fingerprint contract and watermark-resistance tests."""

from __future__ import annotations

import asyncio
import io
import json

import httpx
from PIL import Image, ImageDraw

from greeceapt.scraper import fingerprints, util as scraper_util


def _pattern(width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 17 + y * 3) % 256, (x * 5 + y * 19) % 256, (x * y) % 256)
    return image


def test_v2_fingerprint_serializes_as_fixed_width_hex():
    value = fingerprints.PhotoFingerprint(version="phash-v2", full=1, center=255)

    assert value.to_json() == {
        "version": "phash-v2",
        "full": "0000000000000001",
        "center": "00000000000000ff",
    }
    assert fingerprints.PhotoFingerprint.from_json(value.to_json()) == value


def test_legacy_hex_parses_as_full_only_fingerprint():
    assert fingerprints.PhotoFingerprint.from_json("00000000000000ff") == (
        fingerprints.PhotoFingerprint(version="phash-v1", full=255, center=None)
    )


def test_malformed_fingerprint_records_are_rejected():
    invalid = (
        None,
        "not-hex",
        {},
        {"version": "phash-v2", "full": "zz", "center": "00"},
        {"version": "unknown", "full": "00", "center": "00"},
        {"version": "phash-v2", "full": "00"},
    )

    assert [fingerprints.PhotoFingerprint.from_json(value) for value in invalid] == [None] * len(invalid)


def test_center_fingerprint_ignores_outer_watermark_region():
    original = _pattern(128, 96)
    watermarked = original.copy()
    draw = ImageDraw.Draw(watermarked)
    border_x = int(original.width * 0.125)
    border_y = int(original.height * 0.125)
    draw.rectangle((0, 0, original.width - 1, border_y - 1), fill="white")
    draw.rectangle((0, original.height - border_y, original.width - 1, original.height - 1), fill="black")
    draw.rectangle((0, 0, border_x - 1, original.height - 1), fill="white")
    draw.rectangle((original.width - border_x, 0, original.width - 1, original.height - 1), fill="black")

    before = fingerprints.fingerprint_image(original)
    after = fingerprints.fingerprint_image(watermarked)

    assert before.full != after.full
    assert before.center is not None and after.center is not None
    assert (before.center ^ after.center).bit_count() < 5


def test_tiny_and_narrow_images_keep_a_nonempty_center_crop():
    for size in ((1, 8), (8, 1), (17, 101)):
        value = fingerprints.fingerprint_image(_pattern(*size))
        assert value.version == "phash-v2"
        assert value.full is not None
        assert value.center is not None


def test_standalone_cache_roundtrips_v2_and_loads_legacy(tmp_path):
    path = tmp_path / "phash_cache.json"
    value = fingerprints.PhotoFingerprint("phash-v2", 0xAAAA, 0xBBBB)
    scraper_util.save_standalone_phash_cache({"photo-1": value}, path)
    assert scraper_util.load_standalone_phash_cache(path) == {"photo-1": value}
    assert json.loads(path.read_text(encoding="utf-8"))["photo-1"]["version"] == "phash-v2"

    path.write_text(json.dumps({"photo-1": "00000000000000ff"}), encoding="utf-8")
    assert scraper_util.load_standalone_phash_cache(path) == {
        "photo-1": fingerprints.PhotoFingerprint("phash-v1", 255, None)
    }


def test_saved_full_only_cache_record_remains_readable_for_lazy_upgrade(tmp_path):
    path = tmp_path / "phash_cache.json"
    scraper_util.save_standalone_phash_cache({"photo-1": "00000000000000ff"}, path)

    assert scraper_util.load_standalone_phash_cache(path) == {
        "photo-1": fingerprints.PhotoFingerprint("phash-v1", 255, None)
    }


def test_standalone_cache_missing_or_corrupt_returns_empty(tmp_path):
    assert scraper_util.load_standalone_phash_cache(tmp_path / "missing.json") == {}
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert scraper_util.load_standalone_phash_cache(path) == {}


def test_listing_cache_prefers_v2_over_legacy_hash(tmp_path, monkeypatch):
    standalone = tmp_path / "phash_cache.json"
    scraper_util.save_standalone_phash_cache({}, standalone)
    monkeypatch.setattr(scraper_util.paths, "PHASH_CACHE_JSON", standalone)
    listings = tmp_path / "listings.json"
    listings.write_text(
        json.dumps([{
            "photo_url_hashes": {"photo-1": "0000000000000001"},
            "photo_fingerprints": {
                "photo-1": {
                    "version": "phash-v2",
                    "full": "0000000000000002",
                    "center": "0000000000000003",
                }
            },
        }]),
        encoding="utf-8",
    )

    assert scraper_util.load_url_hash_cache_from_listings_json(listings)["photo-1"] == (
        fingerprints.PhotoFingerprint("phash-v2", 2, 3)
    )


def test_legacy_cache_entry_is_downloaded_and_upgraded():
    image = Image.new("RGB", (32, 32), color=(20, 40, 60))
    body = io.BytesIO()
    image.save(body, format="PNG")
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=body.getvalue(), request=request)

    cache = {
        "https://images.test/one.png": fingerprints.PhotoFingerprint("phash-v1", 1, None)
    }

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await scraper_util.compute_listing_photo_fingerprints(
                list(cache), cache, asyncio.Semaphore(1), client
            )

    result = asyncio.run(run())

    assert calls == ["https://images.test/one.png"]
    assert result["https://images.test/one.png"].is_complete_v2
    assert cache == result
