"""
Layer 1 — Visual Auditor (Moondream2 via Ollama).

Aggregation strategy: scan ALL images for a listing, collect modern_score for
every image where is_correct_room is True, then return the average of the
top-5 scores rounded to 2 decimal places.

Images are resized to 384×384 (LANCZOS) before being sent to Ollama to reduce
payload size and normalise the model's field of view.
"""

from __future__ import annotations

import base64
import logging
import tempfile
from io import BytesIO
import gc

import requests as http_requests
from PIL import Image

OLLAMA_BASE_URL  = "http://localhost:11434"
MOONDREAM_MODEL  = "moondream"
OLLAMA_TIMEOUT_S = 60
IMAGE_SIZE       = (384, 384)
TOP_K            = 5

logger = logging.getLogger(__name__)


# ── Image helpers ─────────────────────────────────────────────────────────────

def download_and_resize(url: str) -> str | None:
    """Download image URL, resize to IMAGE_SIZE LANCZOS, save to temp JPEG. Returns path or None."""
    try:
        r = http_requests.get(url.strip(), timeout=15)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB").resize(IMAGE_SIZE, Image.LANCZOS)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
            img.save(f, format="JPEG", quality=85)
            return f.name
    except Exception:
        logger.debug("Image fetch/resize failed: %s", url, exc_info=True)
        return None


# ── Moondream inference ───────────────────────────────────────────────────────

_POSITIVE_WORDS = {
    "modern", "contemporary", "renovated", "elegant", "luxurious", "pristine",
    "immaculate", "stylish", "upscale", "hardwood", "parquet", "marble",
}
_GOOD_WORDS = {
    "clean", "bright", "spacious", "inviting", "comfortable", "well-maintained",
    "organized", "nice", "beautiful", "cozy", "functional", "good", "tidy",
    "pleasant", "attractive", "warm", "natural light", "well-lit",
}
_BAD_WORDS = {
    "worn", "old", "damaged", "dirty", "dark", "rundown", "dilapidated",
    "broken", "abandoned", "disrepair", "stained", "cramped", "cluttered",
    "outdated", "neglected", "deteriorated", "empty", "bare", "disarray",
    "concrete", "unfinished",
}
_EXTERIOR_WORDS = {
    "exterior", "street", "road", "map", "floor plan", "building facade",
    "outside", "garden", "yard", "balcony view", "aerial",
}


def _score_from_description(desc: str) -> float:
    """Convert a free-text description into a 1–10 quality score via keyword matching."""
    words = desc.lower()
    score = 5.0
    for w in _POSITIVE_WORDS:
        if w in words:
            score += 1.5
    for w in _GOOD_WORDS:
        if w in words:
            score += 0.5
    for w in _BAD_WORDS:
        if w in words:
            score -= 1.5
    return round(max(1.0, min(10.0, score)), 2)


_MERGED_PROMPT = "Briefly describe this room: its type and how clean and modern it looks."
_NUM_PREDICT   = 80


def _run_moondream(image_path: str) -> dict | None:
    """Send an image to Moondream and return a parsed result dict."""
    gc.collect()

    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        resp = http_requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": MOONDREAM_MODEL,
                "prompt": _MERGED_PROMPT,
                "images": [img_b64],
                "stream": False,
                "options": {"num_predict": _NUM_PREDICT},
            },
            timeout=OLLAMA_TIMEOUT_S,
        )
        del img_b64
        resp.raise_for_status()
        desc = resp.json().get("response", "").strip()
        resp.close()

        if not desc:
            return None

        desc_lower = desc.lower()
        is_exterior = any(w in desc_lower for w in _EXTERIOR_WORDS)
        room = "exterior" if is_exterior else "interior"

        score = _score_from_description(desc_lower)
        return {"room": room, "score": score, "desc": desc[:120]}

    except Exception as e:
        logger.error("Moondream error: %s", e)
        return None
    finally:
        gc.collect()

# ── Public entry point ────────────────────────────────────────────────────────
def analyze_listing(image_paths: list[str]) -> float | None:
    scores: list[float] = []

    for i, path in enumerate(image_paths, 1):
        result = _run_moondream(path)

        if not result:
            logger.info("  img %d/%d  FAILED", i, len(image_paths))
            continue

        is_interior = result.get("room") == "interior"
        score = float(result.get("score", 0))

        if is_interior and 0 < score <= 10:
            scores.append(score)
            logger.info("  img %d/%d KEEP | Score: %.1f", i, len(image_paths), score)
        else:
            logger.info("  img %d/%d SKIP | exterior", i, len(image_paths))

    if not scores:
        return None

    top_k = sorted(scores, reverse=True)[:TOP_K]
    return round(sum(top_k) / len(top_k), 2)
